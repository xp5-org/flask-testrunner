import unittest
import coverage
import multiprocessing
import time
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, NoSuchElementException

import sys
import app
import dbhelper


SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8080
SELENIUM_URL = "http://192.168.1.2:4444/wd/hub"
BASE_URL = "http://192.168.1.2:8088/"




class UnitTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.flask_app = app.app
        cls.flask_app.testing = True
        cls.db = dbhelper.db

    def setUp(self):
        self.client = self.flask_app.test_client()

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
        this tries the main home page and looks for the string "Available Tests". 
        if that is found and return code is not 404 or 500, then consider it working
        '''
        driver = self.driver
        driver.set_page_load_timeout(5)
        driver.get("http://192.168.1.2:8088/")

        page_source = driver.page_source

        self.assertTrue(len(page_source) > 0)
        self.assertNotIn("404 Not Found", page_source)
        self.assertNotIn("500 Internal Server Error", page_source)
        self.assertNotIn("ERR_CONNECTION", page_source)

        body = driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body)
        self.assertIn("Available Tests", body.text)


    def test_action_list_matches_backend(self):
        '''
        this function tests the input info which the app.py flask testbuilder route uses to send the 'action list'
        src is a list of available functions (tests) to be run which are located in dispatchhelper.py of /testsrc/pyhelpers
        this checks if js can unpack the info it queies and validates whether the list it shows has the same entries as the list it sourced from

        purpose of this is to validate js and template are working
        - testbuilder
        -- testbuilder's 'test step action list
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
        this tries the main home page and looks for the string "Available Tests". 
        if that is found and return code is not 404 or 500, then consider it working
        '''
        driver = self.driver
        driver.set_page_load_timeout(5)
        driver.get("http://192.168.1.2:8088/test/C128%2040%20Column%20Text")

        page_source = driver.page_source

        self.assertTrue(len(page_source) > 0)
        self.assertNotIn("404 Not Found", page_source)
        self.assertNotIn("500 Internal Server Error", page_source)
        self.assertNotIn("ERR_CONNECTION", page_source)

        body = driver.find_element(By.TAG_NAME, "body")
        self.assertIsNotNone(body)
        self.assertIn("Test Details", body.text)



if __name__ == "__main__":
    import threading
    import time
    import coverage
    import unittest

    cov = coverage.Coverage(branch=True, omit=["/testsrc/sourcedir/*"])
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

    selenium_result = None
    if unit_result.wasSuccessful():
        print("\n--- Starting Selenium Tests ---")
        try:
            selenium_suite = loader.loadTestsFromTestCase(SeleniumTests)
            selenium_result = runner.run(selenium_suite)
        except Exception as e:
            print(f"Selenium Suite crashed: {e}")
    else:
        print("\nSkipping Selenium Tests because Unit Tests failed.")

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
    print_summary("Selenium Tests", selenium_result)
    print("="*30)

    cov.stop()
    cov.save()
    print("\n--- Coverage Report ---")
    cov.report()
    cov.html_report(directory="htmlcov")
