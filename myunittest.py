import unittest
import time
import sys

# Optional deps: the browser/coverage stack isn't installed everywhere the
# runnable API tests need to execute. Guard the imports so importing this
# module (and running ApiV1Tests) never requires Selenium/coverage/bs4; the
# tests that do need them skip cleanly when they're missing.
try:
    import coverage
except ImportError:
    coverage = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

import app
import dbhelper


SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8080
SELENIUM_URL = "http://192.168.1.2:4444/wd/hub"
BASE_URL = "http://192.168.1.2:8088/"

# Canonical test id = module dot-path (the key in apphelpers.testfile_registry
# and the id used everywhere in /api/v1). This test exists in /testsrc/sourcedir.
TEST_ID = "c128src.textprint40col.__testlist__c128_40coltext"


# ─────────────────────────────────────────────────────────────────────────────
# TESTING GAPS (as of the /api/v1 migration)
#
# Covered here without a browser (Flask test_client, ApiV1Tests):
#   - discovery index, actions catalog, tests list, test detail + steps,
#     faithful edit round-trip (PUT), validate, reports list/read,
#     uniform error envelopes (400/404).
#
# NOT covered automatically (documented as @unittest.skip stubs below):
#   - Full run -> report lifecycle (POST /runs -> poll -> GET /reports/{id}):
#     launches VICE and needs an X display, so it is excluded from the headless
#     suite. Verified manually in the container.
#   - Browser coverage that the migrated templates' JS actually drives the v1
#     endpoints end to end: index run button -> /api/v1/runs, testbuilder save
#     -> PUT /api/v1/tests/{id}/steps, cloneproj clone -> POST /api/v1/tests.
#     The Selenium tests here assert page + dynamic-content load, not the full
#     mutating flows.
#   - Concurrency guard (409 while a run is active) — API-level, but omitted
#     from the headless suite because it requires an active run.
# ─────────────────────────────────────────────────────────────────────────────


class UnitTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.flask_app = app.app
        cls.flask_app.testing = True
        cls.db = dbhelper.db

    def setUp(self):
        self.client = self.flask_app.test_client()

    @unittest.skipUnless(BeautifulSoup, "beautifulsoup4 not installed")
    def test_navbar_button_from_db(self):
        '''
        this tests the / rotue in app.py
        if flask returns a html template with the navbar button
        it doesnt validate what is received, only that app.py starts without crashing
        '''
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

        soup = BeautifulSoup(response.data, 'html.parser')
        button = soup.select_one('a.navbutton:nth-child(1)')

        self.assertIsNotNone(button)
        self.assertEqual(button.get('href'), '/')
        self.assertIn('Home', button.get_text())


class ApiV1Tests(unittest.TestCase):
    '''
    Exercises the /api/v1 REST surface in-process via the Flask test client.
    These need /testsrc mounted (for the test registry) but need no browser or
    Selenium, so they run in the container as-is. Every check mirrors a shape
    the migrated templates depend on.
    '''

    @classmethod
    def setUpClass(cls):
        cls.flask_app = app.app
        cls.flask_app.testing = True

    def setUp(self):
        self.client = self.flask_app.test_client()

    # ── discovery / reference ────────────────────────────────────────────────

    def test_index_is_self_describing(self):
        resp = self.client.get('/api/v1')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn('data', body)
        self.assertIn('links', body)
        for key in ('workflow', 'resources', 'data_model', 'envelope'):
            self.assertIn(key, body['data'])

    def test_actions_match_backend(self):
        '''
        The /api/v1/actions catalog must expose exactly the dispatch functions
        available to steps (same source the testbuilder page renders). This is
        the API-level equivalent of SeleniumTests.test_action_list_matches_backend.
        '''
        import dispatchhelper
        resp = self.client.get('/api/v1/actions')
        self.assertEqual(resp.status_code, 200)
        actions = resp.get_json()['data']['actions']
        self.assertGreater(len(actions), 0)

        api_names = {a['action'] for a in actions}
        backend_names = set(dispatchhelper.load_step_dispatch('/testsrc/pyhelpers').keys())
        self.assertEqual(
            api_names, backend_names,
            "actions endpoint drifted from dispatch: missing=%s extra=%s"
            % (backend_names - api_names, api_names - backend_names))

        for a in actions:
            self.assertIn('params', a)
            self.assertIn('is_teststep', a)

    # ── tests: discovery + edit lifecycle ────────────────────────────────────

    def test_tests_list_shape(self):
        resp = self.client.get('/api/v1/tests')
        self.assertEqual(resp.status_code, 200)
        tests = resp.get_json()['data']['tests']
        self.assertGreater(len(tests), 0)
        for key in ('id', 'display_name', 'types', 'system', 'platform',
                    'latest_status', 'links'):
            self.assertIn(key, tests[0])
        # canonical id is the module dot-path
        self.assertTrue(any('__testlist__' in t['id'] for t in tests))

    def test_test_detail_and_steps_agree(self):
        resp = self.client.get('/api/v1/tests/%s' % TEST_ID)
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.get_json()['data']
        self.assertEqual(data['id'], TEST_ID)
        self.assertIsInstance(data['config'], dict)
        self.assertIsInstance(data['steps'], list)

        sresp = self.client.get('/api/v1/tests/%s/steps' % TEST_ID)
        self.assertEqual(sresp.status_code, 200)
        self.assertEqual(sresp.get_json()['data']['steps'], data['steps'])

    def test_steps_faithful_roundtrip(self):
        '''
        GET steps -> PUT them back unchanged must be faithful: no schema-default
        backfill, no param migration (per the raw edit-path decision), and the
        write is idempotent. Snapshots and restores the .py file so the test
        leaves no changes on disk.
        '''
        import apphelpers
        self.client.get('/api/v1/tests')  # ensure registry populated
        meta = apphelpers.testfile_registry.get(TEST_ID)
        self.assertIsNotNone(meta)
        path = meta['__full_path__']
        with open(path, 'rb') as fh:
            original = fh.read()
        try:
            steps = self.client.get('/api/v1/tests/%s/steps' % TEST_ID) \
                        .get_json()['data']['steps']
            keys_before = [sorted(s.get('param', {})) for s in steps]

            put = self.client.put('/api/v1/tests/%s/steps' % TEST_ID,
                                  json={'steps': steps})
            self.assertEqual(put.status_code, 200)
            with open(path, 'rb') as fh:
                after_first = fh.read()

            steps2 = self.client.get('/api/v1/tests/%s/steps' % TEST_ID) \
                         .get_json()['data']['steps']
            keys_after = [sorted(s.get('param', {})) for s in steps2]
            self.assertEqual(keys_before, keys_after,
                             "param keys changed -> backfill/migration leaked in")

            self.client.put('/api/v1/tests/%s/steps' % TEST_ID,
                            json={'steps': steps2})
            with open(path, 'rb') as fh:
                after_second = fh.read()
            self.assertEqual(after_first, after_second, "write not idempotent")
        finally:
            with open(path, 'wb') as fh:
                fh.write(original)

    def test_validate_on_disk_config(self):
        resp = self.client.post('/api/v1/tests/%s/validate' % TEST_ID, json={})
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.get_json()['data']
        self.assertIn('has_errors', data)
        self.assertIsInstance(data['results'], list)

    # ── reports ──────────────────────────────────────────────────────────────

    def test_reports_list_and_read(self):
        resp = self.client.get('/api/v1/reports')
        self.assertEqual(resp.status_code, 200)
        reports = resp.get_json()['data']['reports']
        if not reports:
            self.skipTest("no reports in report.sqlite yet")
        rid = reports[0]['report_id']
        r = self.client.get('/api/v1/reports/%d' % rid)
        self.assertEqual(r.status_code, 200)
        rdata = r.get_json()['data']
        for key in ('report_id', 'status', 'results', 'artifacts'):
            self.assertIn(key, rdata)

    # ── error envelope contract ──────────────────────────────────────────────

    def test_error_envelope(self):
        # unknown test -> 404 with {"error": {"code", "message"}}
        r = self.client.get('/api/v1/tests/does.not.exist')
        self.assertEqual(r.status_code, 404)
        self.assertIn('error', r.get_json())
        self.assertIn('code', r.get_json()['error'])

        # run request missing 'test' -> 400 (no run launched)
        r = self.client.post('/api/v1/runs', json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn('error', r.get_json())

        # unknown run id -> 404
        self.assertEqual(self.client.get('/api/v1/runs/nope').status_code, 404)
        # unknown report id -> 404
        self.assertEqual(self.client.get('/api/v1/reports/99999999').status_code, 404)

    # ── run -> report lifecycle (GAP: needs VICE + display) ──────────────────

    @unittest.skip("requires VICE + X display; verified manually in container")
    def test_full_run_report_lifecycle(self):
        '''
        POST /api/v1/runs -> poll GET /api/v1/runs/{id} until status != running
        -> GET /api/v1/reports/{report_id}. Launches VICE, so it is excluded
        from the headless suite. Left as an executable stub documenting intent.
        '''
        start = self.client.post('/api/v1/runs', json={'test': TEST_ID})
        self.assertEqual(start.status_code, 202)
        run_id = start.get_json()['data']['run_id']

        report_id = None
        for _ in range(60):
            time.sleep(2)
            row = self.client.get('/api/v1/runs/%s' % run_id).get_json()['data']
            if row['status'] != 'running':
                report_id = row.get('report_id')
                break
        self.assertIsNotNone(report_id)
        rep = self.client.get('/api/v1/reports/%d' % report_id).get_json()['data']
        self.assertIn(rep['status'], ('PASS', 'FAIL'))
        self.assertTrue(rep['results'])


@unittest.skipUnless(SELENIUM_AVAILABLE, "selenium not installed / no browser")
class SeleniumTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")

        cls.driver = webdriver.Remote(
            command_executor=SELENIUM_URL,
            options=chrome_options
        )

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "driver"):
            cls.driver.quit()


    def test_homepage_page_loads(self):
        '''
        Loads the home page and confirms the test list actually populates from
        GET /api/v1/tests (JS-driven), not just that the static shell rendered.
        '''
        driver = self.driver
        driver.set_page_load_timeout(10)
        driver.get("http://192.168.1.2:8088/")

        page_source = driver.page_source

        self.assertTrue(len(page_source) > 0)
        self.assertNotIn("404 Not Found", page_source)
        self.assertNotIn("500 Internal Server Error", page_source)
        self.assertNotIn("ERR_CONNECTION", page_source)

        body = driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body)
        self.assertIn("Available Tests", body.text)

        # Dynamic content: the test table is rendered from GET /api/v1/tests.
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".test-link")))
        links = driver.find_elements(By.CSS_SELECTOR, ".test-link")
        self.assertGreater(len(links), 0,
                           "test list did not populate from /api/v1/tests")


    def test_action_list_matches_backend(self):
        '''
        this function tests the input info which the app.py flask testbuilder route uses to send the 'action list'
        src is a list of available functions (tests) to be run which are located in dispatchhelper.py of /testsrc/pyhelpers
        this checks if js can unpack the info it queies and validates whether the list it shows has the same entries as the list it sourced from

        purpose of this is to validate js and template are working
        - testbuilder
        -- testbuilder's 'test step action list

        NB: the API-level equivalent of this check is
        ApiV1Tests.test_actions_match_backend, which does not need a browser.
        '''
        import dispatchhelper
        driver = self.driver
        driver.get("http://192.168.1.2:8088/testbuilder/c128src.textprint40col.__testlist__c128_40coltext")

        wait = WebDriverWait(driver, 10)

        try:
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "action-item")))
        except TimeoutException:
            print(driver.page_source)
            raise

        action_items = driver.find_elements(By.CLASS_NAME, "action-item")
        js_actions = set(item.get_attribute("data-action") for item in action_items)

        self.assertGreater(len(js_actions), 0)

        proj_dir = "/testsrc/pyhelpers"
        raw_dispatch = dispatchhelper.load_step_dispatch(proj_dir)
        schemas = dispatchhelper.PROJECT_STEP_SCHEMAS.get(proj_dir, {})

        dispatch = {}
        for name, func in raw_dispatch.items():
            if getattr(func, "_is_teststep", False):
                dispatch[name] = func

        output = {"functions": list(dispatch.keys()), "schemas": schemas}

        server_schema = {}
        for func_name in output["functions"]:
            server_schema[func_name] = output["schemas"].get(func_name, {})

        server_actions = set(server_schema.keys())

        missing_in_js = server_actions - js_actions
        extra_in_js = js_actions - server_actions

        self.assertFalse(missing_in_js, "Missing in JS: %s" % missing_in_js)
        self.assertFalse(extra_in_js, "Unexpected in JS: %s" % extra_in_js)


    def test_testviewpage(self):
        '''
        Loads a test detail page and confirms the heading renders. Uses the
        canonical module dot-path id — the same id index.html now puts on each
        test-link (data-testid) and navigates to.
        '''
        driver = self.driver
        driver.set_page_load_timeout(5)
        driver.get("http://192.168.1.2:8088/test/c128src.textprint40col.__testlist__c128_40coltext")

        page_source = driver.page_source

        self.assertTrue(len(page_source) > 0)
        self.assertNotIn("404 Not Found", page_source)
        self.assertNotIn("500 Internal Server Error", page_source)
        self.assertNotIn("ERR_CONNECTION", page_source)

        body = driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body)
        self.assertIn("Test Details", body.text)


    # ── Browser coverage gaps (require Selenium + a live run) ────────────────

    @unittest.skip("GAP: no browser coverage of the run flow (needs Selenium + VICE)")
    def test_run_flow_from_index(self):
        '''
        Click a run button on / and confirm the JS drives POST /api/v1/runs and
        that #progress-status updates from polling GET /api/v1/runs/current until
        the run completes. Documents an untested end-to-end path.
        '''
        pass

    @unittest.skip("GAP: no browser coverage of edit/save (needs Selenium)")
    def test_testbuilder_edit_and_save(self):
        '''
        Select a test, edit a step param, click Save, and confirm the JS issues
        PUT /api/v1/tests/{id}/steps and reports success. Documents an untested
        mutating path.
        '''
        pass



if __name__ == "__main__":
    import threading
    import time
    import unittest

    cov = coverage.Coverage(branch=True, omit=["/testsrc/sourcedir/*"]) if coverage else None
    if cov:
        cov.start()

    # Start Flask server in a daemon thread so it runs in the same process as coverage
    server_thread = threading.Thread(target=lambda: app.app.run(
        host=SERVER_HOST,
        port=SERVER_PORT,
        debug=False,
        use_reloader=False
    ), daemon=True)
    server_thread.start()

    time.sleep(2)
    if not server_thread.is_alive():
        print("Error: Flask server failed to start.")
        if cov:
            cov.stop()
            cov.save()
            cov.report()
            cov.html_report(directory="htmlcov")
        exit(1)

    loader = unittest.TestLoader()
    runner = unittest.TextTestRunner(verbosity=2)

    print("\n--- Starting Unit Tests ---")
    unit_suite = loader.loadTestsFromTestCase(UnitTests)
    unit_result = runner.run(unit_suite)

    print("\n--- Starting API (v1) Tests ---")
    api_suite = loader.loadTestsFromTestCase(ApiV1Tests)
    api_result = runner.run(api_suite)

    selenium_result = None
    if unit_result.wasSuccessful() and api_result.wasSuccessful():
        print("\n--- Starting Selenium Tests ---")
        try:
            selenium_suite = loader.loadTestsFromTestCase(SeleniumTests)
            selenium_result = runner.run(selenium_suite)
        except Exception as e:
            print(f"Selenium Suite crashed: {e}")
    else:
        print("\nSkipping Selenium Tests because Unit/API Tests failed.")

    # No terminate/join needed; thread is daemonized
    print("\n" + "="*30)
    print("FINAL TEST SUMMARY")
    print("="*30)

    def print_summary(name, result):
        if result:
            status = "PASSED" if result.wasSuccessful() else "FAILED"
            print(f"{name}: {status}")
            print(f"    Run: {result.testsRun}")
            print(f"    Errors: {len(result.errors)}")
            print(f"    Failures: {len(result.failures)}")
        else:
            print(f"{name}: SKIPPED")

    print_summary("Unit Tests", unit_result)
    print_summary("API (v1) Tests", api_result)
    print_summary("Selenium Tests", selenium_result)
    print("="*30)

    if cov:
        cov.stop()
        cov.save()
        print("\n--- Coverage Report ---")
        cov.report()
        cov.html_report(directory="htmlcov")
