import json
import time
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock

from he_app.domain.models import Site, SourceDocument
from he_app.domain.errors import SiteScrapeFailure
from he_app.fetch.url_policy import StrictNetworkPolicy, same_origin_url, url_origin


def advance_wait_state(
    current_state: tuple[int, int],
    previous_state: tuple[int, int] | None,
    last_state: tuple[int, int] | None,
    stable_count: int,
) -> tuple[int, bool]:
    body_len, page_len = current_state
    if body_len < 20 and page_len < 200:
        return 0, False
    if previous_state is not None and current_state == previous_state:
        return 0, False
    next_count = stable_count + 1 if current_state == last_state else 1
    return next_count, next_count >= 2


class BrowserClient:
    def __init__(self, headless: bool = True, network_policy=None):
        self.headless = headless
        self.driver = None
        self.capture_sequence = 0
        self.network_policy = network_policy if network_policy is not None else StrictNetworkPolicy(require_peer=True)
        self.last_peer_ip = ""
        self.last_resolved_addresses: tuple[str, ...] = ()

    def start(self) -> None:
        if self.driver is not None:
            return
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.edge.options import Options as EdgeOptions

        binary = self.find_browser_binary()
        is_edge = bool(binary and Path(binary).name.lower().startswith("msedge"))
        options = EdgeOptions() if is_edge else ChromeOptions()
        options.page_load_strategy = "eager"
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,2400")
        options.add_argument("--no-proxy-server")
        options.add_argument("--proxy-bypass-list=*")
        options.add_argument("--disable-quic")
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        if binary:
            options.binary_location = binary
        factory = webdriver.Edge if is_edge else webdriver.Chrome
        self.driver = factory(options=options)

    @staticmethod
    def find_browser_binary() -> str | None:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        return next((candidate for candidate in candidates if Path(candidate).exists()), None)

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    def clear_site_state(self) -> None:
        if self.driver is None:
            return
        self.driver.delete_all_cookies()
        self.driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
        self.driver.get("about:blank")

    def get_document_state(self) -> tuple[int, int]:
        body_text = ""
        page_source = ""
        if self.driver is not None:
            try:
                body_text = self.driver.find_element("tag name", "body").text or ""
            except Exception:
                pass
            try:
                page_source = self.driver.page_source or ""
            except Exception:
                pass
        return len(body_text.strip()), len(page_source)

    def wait_for_document(self, max_wait: float, previous_state: tuple[int, int] | None = None) -> None:
        deadline = time.monotonic() + max(0.0, max_wait)
        last_state = None
        stable_count = 0
        while True:
            state = self.get_document_state()
            stable_count, ready = advance_wait_state(state, previous_state, last_state, stable_count)
            if ready or time.monotonic() >= deadline:
                return
            last_state = state
            time.sleep(0.25)


    def _clear_performance_log(self) -> None:
        if self.driver is None or not hasattr(self.driver, "get_log"):
            return
        try:
            self.driver.get_log("performance")
        except Exception:
            pass

    def _document_remote_ips(self, expected_url: str) -> list[str]:
        if self.driver is None or not hasattr(self.driver, "get_log"):
            return []
        expected_origin = url_origin(expected_url)
        addresses: list[str] = []
        try:
            entries = self.driver.get_log("performance")
        except Exception:
            return []
        for entry in entries:
            try:
                message = json.loads(entry.get("message", "{}"))["message"]
                if message.get("method") != "Network.responseReceived":
                    continue
                params = message.get("params", {})
                response = params.get("response", {})
                if params.get("type") != "Document":
                    continue
                response_url = str(response.get("url", ""))
                if url_origin(response_url) != expected_origin:
                    continue
                address = str(response.get("remoteIPAddress", "")).strip()
                if address and address not in addresses:
                    addresses.append(address)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, SiteScrapeFailure):
                continue
        return addresses

    def _verify_navigation_network(self, requested_url: str, resolved) -> None:
        if self.driver is None:
            raise RuntimeError("Browser is not started")
        final_url = self.driver.current_url
        same_origin_url(requested_url, final_url)
        final_resolved = resolved
        addresses = self._document_remote_ips(final_url)
        if self.network_policy is not None and not addresses:
            raise SiteScrapeFailure("站点身份错误", f"浏览器无法验证实际连接公网地址: {final_url}")
        verified = [
            self.network_policy.verify_address(
                address,
                context=f"浏览器连接 {final_resolved.host}",
                resolved=final_resolved,
            )
            for address in addresses
        ] if self.network_policy is not None else addresses
        self.last_peer_ip = verified[-1] if verified else ""
        self.last_resolved_addresses = tuple(final_resolved.addresses) if final_resolved is not None else ()

    def get_documents(self, url: str, period: int, click_first: bool, timeout: int) -> list[str]:
        if self.driver is None:
            raise RuntimeError("Browser is not started")
        url_origin(url)
        resolved = self.network_policy.resolve(url) if self.network_policy is not None else None
        self._clear_performance_log()
        self.driver.set_page_load_timeout(timeout)
        self.driver.get(url)
        same_origin_url(url, self.driver.current_url)
        self.wait_for_document(min(float(timeout), 5.0))
        self.capture_sequence = 0
        if click_first:
            previous = self.get_document_state()
            if self.try_click_labels():
                same_origin_url(url, self.driver.current_url)
                self.wait_for_document(min(float(timeout), 2.0), previous)
            previous = self.get_document_state()
            if not self.try_click_best_post(period):
                raise SiteScrapeFailure("主页找帖失败", "没有唯一的同源指定期严格文章链接")
            same_origin_url(url, self.driver.current_url)
            self.wait_for_document(min(float(timeout), 3.0), previous)
        self._verify_navigation_network(url, resolved)
        # Navigation snapshots and list summaries are diagnostics, not alternate
        # authorities that may rescue a failed final detail page.
        documents = self.collect_documents(url)
        return [documents[-1]] if documents else []

    def collect_documents(self, url: str = "") -> list[str]:
        if self.driver is None:
            return []
        try:
            body_text = self.driver.find_element("tag name", "body").text
        except Exception:
            body_text = ""
        original_url = url
        url = self.driver.current_url
        if original_url:
            same_origin_url(original_url, url)
        state_id = f"browser:{url}:state:{self.capture_sequence}"
        self.capture_sequence += 1
        return [
            SourceDocument(
                body_text,
                source_url=url,
                fetch_kind="browser",
                document_type="body-text",
                parent_url=original_url,
                authority_id=state_id,
                document_id=f"{state_id}:body",
                peer_ip=self.last_peer_ip,
                resolved_addresses=self.last_resolved_addresses,
            ),
            SourceDocument(
                self.driver.page_source,
                source_url=url,
                fetch_kind="browser",
                document_type="page-source",
                parent_url=original_url,
                authority_id=state_id,
                document_id=f"{state_id}:page-source",
                peer_ip=self.last_peer_ip,
                resolved_addresses=self.last_resolved_addresses,
            ),
        ]

    def try_click_labels(self) -> bool:
        if self.driver is None:
            return False
        script = r"""
        const labels = arguments[0];
        for (const label of labels) {
            for (const el of document.querySelectorAll("a,button,li,div,span")) {
                const text = (el.innerText || "").replace(/\s+/g, "").trim();
                if (!text || text.length > 40) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 10 || rect.height < 10) continue;
                if (text === label || text.includes(label)) { el.click(); return true; }
            }
        }
        return false;
        """
        try:
            return bool(self.driver.execute_script(script, ["高手论坛", "论坛", "帖子", "资料"]))
        except Exception:
            return False

    def try_click_best_post(self, period: int) -> bool:
        if self.driver is None:
            return False
        # Only actual navigable links; never score/click arbitrary parent DIVs.
        script = r"""
        const periodRe = new RegExp("(?:^|[^0-9])" + String(arguments[0]) + "\\s*期");
        const keywordRe = /绝\s*杀\s*一\s*合|公式\s*杀\s*合|绝\s*杀\s*合/;
        const links = new Map();
        for (const el of document.querySelectorAll("a[href]")) {
            const text = (el.innerText || "").trim();
            const rect = el.getBoundingClientRect();
            if (!periodRe.test(text) || !keywordRe.test(text) || rect.width < 1 || rect.height < 1) continue;
            const href = new URL(el.getAttribute("href"), location.href);
            if (!/^https?:$/.test(href.protocol) || href.origin !== location.origin || href.href === location.href) continue;
            links.set(href.href, el);
        }
        if (links.size > 1) return "conflict";
        if (links.size !== 1) return "missing";
        links.values().next().value.click();
        return "clicked";
        """
        status = self.driver.execute_script(script, period)
        if status == "conflict":
            raise SiteScrapeFailure("候选冲突", "浏览器命中多个不同目标文章链接")
        return status == "clicked"


def close_browser_safely(browser: BrowserClient) -> None:
    try:
        browser.close()
    except Exception:
        pass


class BrowserPool:
    def __init__(self, size: int, headless: bool = True, client_factory=None):
        self.size = max(0, size)
        self.headless = headless
        self.client_factory = client_factory or BrowserClient
        self.available: Queue = Queue(maxsize=self.size or 1)
        self.clients: set[BrowserClient] = set()
        self.clients_lock = Lock()
        self.lifecycle_lock = Lock()
        self.failure_token = object()
        self.failure: Exception | None = None
        self.started = False

    def _create_client(self) -> BrowserClient:
        client = self.client_factory(headless=self.headless)
        try:
            client.start()
        except Exception:
            close_browser_safely(client)
            raise
        with self.clients_lock:
            self.clients.add(client)
        return client

    def start(self) -> None:
        with self.lifecycle_lock:
            if self.started:
                return
            if self.size == 0:
                raise RuntimeError("browser pool size must be positive")
            self.available = Queue(maxsize=self.size)
            self.failure = None
            created = []
            try:
                for _ in range(self.size):
                    client = self._create_client()
                    created.append(client)
                    self.available.put(client)
            except Exception as exc:
                self.failure = exc
                self.started = True
                with self.clients_lock:
                    for client in created:
                        self.clients.discard(client)
                for client in created:
                    close_browser_safely(client)
                raise
            self.started = True

    def _mark_failed(self, exc: Exception) -> None:
        with self.clients_lock:
            if self.failure is None:
                self.failure = exc
        try:
            self.available.put_nowait(self.failure_token)
        except Full:
            pass

    def _replace_after_cleanup_failure(self, client: BrowserClient) -> Exception | None:
        with self.clients_lock:
            self.clients.discard(client)
        close_browser_safely(client)
        try:
            replacement = self._create_client()
        except Exception as exc:
            self._mark_failed(exc)
            return exc
        self.available.put(replacement)
        return None

    def run(self, operation):
        if not self.started:
            self.start()
        with self.clients_lock:
            failure = self.failure
        if failure is not None:
            raise failure
        try:
            client = self.available.get(timeout=60.0)
        except Empty as exc:
            raise TimeoutError("浏览器池等待超过60秒") from exc
        if client is self.failure_token:
            self.available.put(self.failure_token)
            with self.clients_lock:
                failure = self.failure
            raise failure or RuntimeError("browser pool unavailable")
        operation_error = None
        try:
            result = operation(client)
        except BaseException as exc:
            operation_error = exc
            result = None
        cleanup_error = None
        with self.clients_lock:
            pool_failed = self.failure is not None
        if pool_failed:
            with self.clients_lock:
                self.clients.discard(client)
            close_browser_safely(client)
        else:
            clear_site_state = getattr(client, "clear_site_state", None)
            if clear_site_state is None:
                self.available.put(client)
            else:
                try:
                    clear_site_state()
                except Exception:
                    cleanup_error = self._replace_after_cleanup_failure(client)
                else:
                    self.available.put(client)
        if operation_error is not None:
            raise operation_error
        if cleanup_error is not None:
            return result
        return result

    def close(self) -> None:
        with self.lifecycle_lock:
            with self.clients_lock:
                clients = list(self.clients)
                self.clients.clear()
            for client in clients:
                close_browser_safely(client)
            self.started = False


def default_browser_pool_size(sites: list[Site], workers: int) -> int:
    from he_app.services.document_sources import requires_browser
    browser_site_count = sum(1 for site in sites if requires_browser(site))
    if browser_site_count == 0:
        return 0
    return min(3, max(1, workers), browser_site_count)
