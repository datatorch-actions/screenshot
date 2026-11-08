"""screenshot action.

Renders a URL or an HTML document in the runner machine's own Google
Chrome (headless, driven by Playwright) and uploads the PNG to a project
path. Inputs arrive as one JSON argument; outputs are `::key::<json>`
lines on stdout. Log lines never contain "::".

The API key is used for the upload and, with auth on, is attached as a
request header by the browser context. It is never exposed to page
JavaScript and never printed.
"""

import json
import os
import posixpath
import struct
import sys
import urllib.parse
import urllib.request
import uuid

DEFAULT_URL = "https://datatorch.io"
DEFAULT_STORAGE_LINK_NAME = "DataTorch Storage"
KEY_HEADER = "datatorch-api-key"


def log(msg: str):
    print(str(msg).replace("::", ": :"), flush=True)


def fail(msg: str, code: int = 1):
    print(str(msg).replace("::", ": :"), file=sys.stderr, flush=True)
    sys.exit(code)


class DataTorch:
    def __init__(self):
        key = (os.getenv("DATATORCH_API_KEY") or "").strip()
        if not key:
            fail("No DataTorch API key: set DATATORCH_API_KEY in the runner's environment.")
        url = (os.getenv("DATATORCH_API_URL") or DEFAULT_URL).strip().rstrip("/")
        for suffix in ("/graphql", "/api"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        self.key = key
        self.url = url

    def request(self, path, data=None, headers=None, timeout=60):
        req = urllib.request.Request(
            f"{self.url}{path}", data=data, headers={KEY_HEADER: self.key, **(headers or {})}
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read()

    def gql(self, query, variables=None):
        raw = self.request(
            "/api/graphql",
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={"content-type": "application/json"},
            timeout=30,
        )
        result = json.loads(raw)
        if result.get("errors"):
            raise RuntimeError(result["errors"][0].get("message"))
        return result["data"]

    def upload(self, link_id, folder, name, content, mime="image/png"):
        boundary = uuid.uuid4().hex
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                    f"Content-Type: {mime}\r\n\r\n"
                ).encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        params = urllib.parse.urlencode({"import": "true", "path": folder})
        self.request(
            f"/api/file/v1/upload/{link_id}?{params}",
            data=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
            timeout=120,
        )


def project_ref(dt, value):
    value = (value or "").strip().strip("/")
    if value:
        if value.count("/") != 1:
            fail("Input 'project' must be <login>/<slug>.")
        return value.split("/")
    project_id = (os.getenv("DATATORCH_PROJECT_ID") or "").strip()
    if not project_id:
        fail("Input 'project' is empty and the runner did not name the run's project (DATATORCH_PROJECT_ID).")
    data = dt.gql(
        "query ($id: ID!) { projectById(id: $id) { slug owner { login } } }", {"id": project_id}
    )
    project = data.get("projectById")
    if not project:
        fail(f"Project {project_id} not found, or the API key cannot read it.")
    return project["owner"]["login"], project["slug"]


def load_project(dt, login, slug, path):
    data = dt.gql(
        """
        query ($login: String!, $slug: String!, $where: DatasetFileWhereInput) {
          project(login: $login, slug: $slug) {
            id
            storageLinkDefault { id }
            storageLinks { id name }
            files(input: { where: $where, perPage: 50 }) { nodes { id path } }
          }
        }
        """,
        {"login": login, "slug": slug, "where": {"path": {"equals": path}}},
    )
    project = data.get("project")
    if not project:
        fail(f"Project {login}/{slug} not found, or the API key cannot read it.")
    link = None
    if project.get("storageLinkDefault"):
        link = project["storageLinkDefault"]["id"]
    else:
        links = sorted(project.get("storageLinks") or [], key=lambda l: l["name"])
        named = [l for l in links if l["name"] == DEFAULT_STORAGE_LINK_NAME]
        chosen = (named or links or [None])[0]
        link = chosen["id"] if chosen else None
    if not link:
        fail(f"Project {login}/{slug} has no storage link to save into.")
    existing = [f for f in (project["files"]["nodes"] or []) if f["path"] == path]
    return link, existing


def find_file(dt, login, slug, path):
    data = dt.gql(
        """
        query ($login: String!, $slug: String!, $where: DatasetFileWhereInput) {
          project(login: $login, slug: $slug) {
            files(input: { where: $where, perPage: 50 }) { nodes { id path } }
          }
        }
        """,
        {"login": login, "slug": slug, "where": {"path": {"equals": path}}},
    )
    nodes = ((data.get("project") or {}).get("files") or {}).get("nodes") or []
    return [f for f in nodes if f["path"] == path]


def png_size(data: bytes):
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def as_bool(v, default=False):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def as_int(v, default):
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


SIGNIN_MARKERS = ("/login", "/signin", "/sign-in", "/auth")


def looks_signed_out(page_url: str) -> bool:
    path = urllib.parse.urlparse(page_url).path.lower()
    return any(m in path for m in SIGNIN_MARKERS)


def render(inputs, key):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail("Playwright is not installed in this action's environment (requirements.txt should bring it).")

    url = (inputs.get("url") or "").strip()
    html = inputs.get("html") or ""
    width = as_int(inputs.get("width"), 1600)
    height = as_int(inputs.get("height"), 1000)
    scale = as_int(inputs.get("scale"), 2)
    full_page = as_bool(inputs.get("full_page"))
    selector = (inputs.get("selector") or "").strip()
    wait_for = (inputs.get("wait_for") or "").strip()
    wait_ms = as_int(inputs.get("wait_ms"), 1500)
    click = (inputs.get("click") or "").strip()
    hide = [s.strip() for s in (inputs.get("hide") or "").split(",") if s.strip()]
    auth = as_bool(inputs.get("auth"))
    timeout_ms = as_int(inputs.get("timeout"), 60) * 1000

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as ex:
            fail(f"Could not start Google Chrome on this runner: {ex}")
        context_args = {
            "viewport": {"width": width, "height": height},
            "device_scale_factor": scale,
            "ignore_https_errors": False,
        }
        if auth and url:
            context_args["extra_http_headers"] = {KEY_HEADER: key}
        context = browser.new_context(**context_args)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        if html and not url:
            log("Rendering the given HTML document")
            page.set_content(html, wait_until="load")
        else:
            log(f"Opening {url}")
            page.goto(url, wait_until="networkidle")
            if auth and looks_signed_out(page.url):
                origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(url))
                log("Landed on a sign-in page; retrying with a client-side route change from the site root")
                page.goto(origin + "/", wait_until="networkidle")
                target = urllib.parse.urlparse(url)
                relative = target.path + (("?" + target.query) if target.query else "")
                page.evaluate(
                    """(u) => {
                        if (window.next && window.next.router) return window.next.router.push(u);
                        if (window.$nuxt && window.$nuxt.$router) return window.$nuxt.$router.push(u);
                        window.location.assign(u);
                    }""",
                    relative,
                )
                page.wait_for_load_state("networkidle")
                if looks_signed_out(page.url):
                    fail("The page still shows a sign-in screen with auth on; the key may not read this project.")

        if hide:
            css = ", ".join(hide) + " { visibility: hidden !important; }"
            page.add_style_tag(content=css)
        if click:
            log(f"Clicking {click}")
            page.click(click)
            page.wait_for_load_state("networkidle")
        if wait_for:
            log(f"Waiting for {wait_for}")
            page.wait_for_selector(wait_for, state="visible")
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)

        if selector:
            element = page.wait_for_selector(selector, state="visible")
            data = element.screenshot(type="png")
        else:
            data = page.screenshot(type="png", full_page=full_page)
        browser.close()
    return data


def main():
    inputs = json.loads(sys.argv[-1])
    path = (inputs.get("path") or "").strip().strip("/")
    if not path or not path.lower().endswith(".png"):
        fail("Input 'path' is required and must end in .png.")
    if ".." in path.split("/"):
        fail("Input 'path' may not contain '..'.")
    if not (inputs.get("url") or "").strip() and not (inputs.get("html") or "").strip():
        fail("One of 'url' or 'html' is required.")
    overwrite = as_bool(inputs.get("overwrite"), True)

    dt = DataTorch()
    login, slug = project_ref(dt, inputs.get("project"))
    link, existing = load_project(dt, login, slug, path)
    if existing and not overwrite:
        fail(f"A file already exists at {path} and overwrite is off.")

    data = render(inputs, dt.key)
    size = png_size(data)
    if not size:
        fail("Chrome did not return a PNG.")
    log(f"Captured {size[0]}x{size[1]} pixels, {len(data) // 1024} KB")

    if existing:
        ids = [f["id"] for f in existing]
        dt.gql("mutation ($ids: [ID!]!) { deleteDatasetFiles(ids: $ids) }", {"ids": ids})
        log(f"Replaced the file already at {path}")

    folder = posixpath.dirname(path)
    name = posixpath.basename(path)
    dt.upload(link, folder, name, data)
    saved = find_file(dt, login, slug, path)
    if not saved:
        fail(f"Upload answered but no file is listed at {path}.")
    file_id = saved[0]["id"]
    log(f"Saved {path} ({file_id})")

    print(f"::path::{json.dumps(path)}")
    print(f"::file_id::{json.dumps(file_id)}")
    print(f"::url::{json.dumps(f'{dt.url}/api/file/v1/{file_id}/download')}")
    print(f"::pixels::{json.dumps(f'{size[0]}x{size[1]}')}")


if __name__ == "__main__":
    main()
