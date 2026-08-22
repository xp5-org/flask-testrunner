import os
import re
import shutil
import hashlib
from collections import defaultdict
from urllib.parse import quote

# Template is expected to live alongside this helper
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "report_template.html")

# Project tree, served read-only by the app at PROJFILES_URL_PREFIX (see the
# /projfiles route in app.py). Artifacts that live under here are linked in
# place instead of being copied into the report — disk images get large.
PROJECT_ROOT = "/testsrc"
PROJFILES_URL_PREFIX = "/projfiles"


def _project_file_url(path: str):
    """If path is inside the project tree, return the /projfiles URL that serves
    it in place; otherwise None (caller should copy it into the report dir)."""
    ap = os.path.abspath(path)
    root = PROJECT_ROOT.rstrip("/")
    if ap != root and not ap.startswith(root + os.sep):
        return None
    rel = os.path.relpath(ap, root)
    # Quote each path segment (spaces etc.) but keep the separators.
    return f"{PROJFILES_URL_PREFIX}/{quote(rel)}"




def _load_template() -> str:
    with open(TEMPLATE_PATH, "r") as f:
        return f.read()


def _build_summary_rows(results: list) -> str:
    rows = []
    for name, status, color, _, _, duration in results:
        rows.append(
            f'<tr><td>{name}</td><td>{duration:.2f}</td>'
            f'<td class="{color}">{status}</td></tr>'
        )
    return "\n  ".join(rows)


def _build_screenshot_map(subdir_path: str) -> defaultdict:
    screenshot_map = defaultdict(list)
    for fname in os.listdir(subdir_path):
        if fname.endswith((".png", ".gif")):
            m = re.match(r"screenshot-[^-]+-(\d+)(?:-\d+)?\.(png|gif)$", fname)
            if m:
                step_num = int(m.group(1))
                screenshot_map[step_num].append(fname)
    return screenshot_map


def _extract_artifacts(output: str) -> list:
    """Pull out file paths logged by build steps as 'ARTIFACT: <path>' lines."""
    artifacts = []
    if not output:
        return artifacts
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("ARTIFACT:"):
            path = line[len("ARTIFACT:"):].strip()
            if path:
                artifacts.append(path)
    return artifacts


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_artifacts(artifact_paths: list, subdir_path: str) -> list:
    """Copy build artifacts (.prg, .d64, .s, .o, ...) into the report subdirectory.

    Copies rather than moves: files like the .d64 are still needed by later
    test steps (e.g. handed to VICE), so the originals must stay in place.

    Returns a list of dicts: {name, size, size_bytes, checksum}.
    """
    copied = []
    seen = set()
    used_names = set()
    for path in artifact_paths:
        if not path or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        # Multi-disk builds can produce identical basenames in different
        # per-disk subfolders (e.g. out/rxclient/main.o and out/txclient/main.o).
        # Disambiguate by prefixing the parent dir name so neither overwrites
        # the other in the flat report directory.
        dest_name = os.path.basename(path)
        if dest_name in used_names:
            parent = os.path.basename(os.path.dirname(path))
            dest_name = f"{parent}_{dest_name}" if parent else dest_name
        used_names.add(dest_name)
        try:
            shutil.copy2(path, os.path.join(subdir_path, dest_name))
        except OSError:
            continue
        size_bytes = os.path.getsize(path)
        copied.append({
            "name": dest_name,
            "size": _format_size(size_bytes),
            "size_bytes": size_bytes,
            "checksum": _sha256_of(path),
        })
    return copied


def _build_artifacts_table(results: list, subdir_path: str) -> str:
    """Build a single consolidated Build Artifacts table for the whole report.

    Aggregates the ARTIFACT: paths logged by every step (deduped) and renders
    one table near the top of the report. Artifacts under the project tree are
    linked in place via the /projfiles route — not copied — so large disk
    images aren't duplicated into the report dir. Anything outside the project
    tree (e.g. a generated launch script) is small and still copied locally.
    """
    all_paths = []
    for (_name, _status, _color, output, _stdout, _duration) in results:
        all_paths.extend(_extract_artifacts(output))

    entries = []          # linked-in-place project files, in first-seen order
    to_copy = []          # everything else, copied into the report dir
    seen = set()
    for path in all_paths:
        if not path or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        url = _project_file_url(path)
        if url is None:
            to_copy.append(path)
            continue
        size_bytes = os.path.getsize(path)
        entries.append({
            "name": os.path.basename(path),
            "href": url,
            "size": _format_size(size_bytes),
            "checksum": _sha256_of(path),
        })

    for a in _copy_artifacts(to_copy, subdir_path):
        entries.append({
            "name": a["name"],
            "href": a["name"],   # relative to the report html's own directory
            "size": a["size"],
            "checksum": a["checksum"],
        })

    if not entries:
        return "<p>No build artifacts.</p>"

    artifact_rows = "\n".join(
        f'<tr><td><a href="{e["href"]}" download>{e["name"]}</a></td>'
        f'<td>{e["size"]}</td>'
        f'<td><code style="font-size:0.85em;">{e["checksum"]}</code></td></tr>'
        for e in entries
    )
    return (
        '<table><tr><th>File</th><th>Size</th><th>SHA-256</th></tr>'
        f'{artifact_rows}</table>'
    )


def _build_detail_sections(results: list, screenshot_map: defaultdict, subdir_path: str) -> str:
    sections = []
    for idx, (name, status, color, output, stdout, duration) in enumerate(results, start=1):
        matching_images = screenshot_map.get(idx, [])
        if matching_images:
            img_tags = "\n".join(
                f'<img src="{img}" alt="{img}" style="max-width: 100%; border: 1px solid #ccc;">'
                for img in matching_images
            )
        else:
            img_tags = "<p>No screenshot available.</p>"

        # Artifacts are rendered once in the consolidated table near the top of
        # the report (see _build_artifacts_table); here we only strip the
        # ARTIFACT: marker lines out of the console output shown per step.
        display_output = "\n".join(
            line for line in (output or "").splitlines() if not line.strip().startswith("ARTIFACT:")
        )

        sections.append(f"""<hr>
<div class="flex-container">
  <div class="output-column">
    <h3>{name}</h3>
    <p><strong>Duration:</strong> {duration:.2f} seconds</p>
    <pre>OUTPUT:
    {display_output}

STDOUT:
{stdout}</pre>
  </div>
  <div class="image-column">
    <h4>Screenshot</h4>
    {img_tags}
  </div>
</div>""")

    return "\n".join(sections)


def _move_assets(report_dir: str, compile_logs_dir: str, subdir_path: str) -> None:
    """Move compile logs and screenshots into the report subdirectory."""
    if os.path.exists(compile_logs_dir):
        for filename in os.listdir(compile_logs_dir):
            shutil.move(os.path.join(compile_logs_dir, filename), subdir_path)

    for filename in os.listdir(report_dir):
        if (re.match(r"test\d+\.(png|ppm|gif)$", filename) or
                filename.startswith("screenshot-")):
            shutil.move(os.path.join(report_dir, filename), subdir_path)


def generate_report(
    results: list,
    report_path: str,
    report_dir: str,
    compile_logs_dir: str,
    testlist_name: str = "",
) -> None:
    """
    Render and write an HTML test report.

    Args:
        results:          List of (name, status, color, output, stdout, duration) tuples.
        report_path:      Full path where the .html file will be written.
        report_dir:       Base report directory (used to locate loose screenshots).
        compile_logs_dir: Directory holding compile logs to include in the report.
        testlist_name:    Human-readable name shown in the report heading.
    """
    subdir_path = os.path.dirname(report_path)
    if subdir_path and not os.path.exists(subdir_path):
        os.makedirs(subdir_path, exist_ok=True)

    _move_assets(report_dir, compile_logs_dir, subdir_path)

    screenshot_map = _build_screenshot_map(subdir_path)
    summary_rows    = _build_summary_rows(results)
    artifacts_table = _build_artifacts_table(results, subdir_path)
    detail_sections = _build_detail_sections(results, screenshot_map, subdir_path)

    html = _load_template()
    html = html.replace("{{testlist_name}}", testlist_name)
    html = html.replace("{{summary_rows}}", summary_rows)
    html = html.replace("{{artifacts_table}}", artifacts_table)
    html = html.replace("{{detail_sections}}", detail_sections)

    with open(report_path, "w") as f:
        f.write(html)

    print(f"Wrote report to {report_path}")