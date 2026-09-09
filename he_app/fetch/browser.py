import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from queue import Full, Queue
from threading import Lock, RLock, get_ident
from typing import TypeVar

from he_app.domain.models import Site, SourceDocument


T = TypeVar("T")


def selector_period_ready(texts: list[str], period: int, anchor: str = "") -> bool:
    period_re = re.compile(rf"(?<!\d){re.escape(str(period))}\s*期")
    compact_anchor = re.sub(r"\s+", "", anchor)
    for text in texts:
        normalized = str(text)
        if compact_anchor and compact_anchor not in re.sub(r"\s+", "", normalized):
            continue
        if period_re.search(normalized):
            return True
    return False


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
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.capture_sequence = 0
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._executor: ThreadPoolExecutor | None = None
        self._executor_lock = Lock()
        self._lifecycle_lock = RLock()
        self._owner_thread_id: int | None = None

    def _ensure_executor(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="he-playwright-browser",
                )
            return self._executor

    def _run_on_owner(self, operation: Callable[[], T]) -> T:
        current_thread_id = get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = current_thread_id
        elif self._owner_thread_id != current_thread_id:
            raise RuntimeError("BrowserClient operation ran on the wrong thread")
        return operation()

    def _dispatch(self, operation: Callable[[], T]) -> T:
        # Nested public calls must stay direct on the owner thread to avoid self-deadlock.
        if self._owner_thread_id == get_ident():
            return operation()
        executor = self._ensure_executor()
        return executor.submit(self._run_on_owner, operation).result()

    def _close_resources_owner(self, suppress_errors: bool = False) -> None:
        errors: list[BaseException] = []
        resources = (
            ("_page", "close"),
            ("_context", "close"),
            ("_browser", "close"),
            ("_playwright", "stop"),
        )
        for attribute, method_name in resources:
            resource = getattr(self, attribute)
            setattr(self, attribute, None)
            if resource is None:
                continue
            try:
                getattr(resource, method_name)()
            except BaseException as exc:
                errors.append(exc)
        if errors and not suppress_errors:
            raise errors[0]

    def _start_owner(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        if any(resource is not None for resource in (self._playwright, self._browser, self._context)):
            self._close_resources_owner(suppress_errors=True)
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            self._context = self._browser.new_context(
                ignore_https_errors=True,
                viewport={"width": 1600, "height": 2400},
            )
            self._page = self._context.new_page()
        except BaseException:
            self._close_resources_owner(suppress_errors=True)
            raise

    def start(self) -> None:
        with self._lifecycle_lock:
            self._dispatch(self._start_owner)

    def close(self) -> None:
        if self._executor is None:
            return
        called_from_owner = self._owner_thread_id == get_ident()
        with self._lifecycle_lock:
            try:
                self._dispatch(lambda: self._close_resources_owner())
            finally:
                self._shutdown_executor(wait=not called_from_owner)

    def _shutdown_executor(self, wait: bool = True) -> None:
        with self._executor_lock:
            executor = self._executor
            self._executor = None
            self._owner_thread_id = None
        if executor is not None:
            executor.shutdown(wait=wait)

    def clear_site_state(self) -> None:
        if self._executor is None:
            return
        self._dispatch(self._clear_site_state_owner)

    def _clear_site_state_owner(self) -> None:
        if self._page is None or self._context is None:
            return
        self._context.clear_cookies()
        self._page.evaluate("() => { window.localStorage.clear(); window.sessionStorage.clear(); }")
        self._page.goto("about:blank", wait_until="domcontentloaded")

    def _get_document_state_owner(self) -> tuple[int, int]:
        body_text = ""
        page_source = ""
        if self._page is not None:
            with suppress(Exception):
                body_text = self._page.locator("body").inner_text() or ""
            with suppress(Exception):
                page_source = self._page.content() or ""
        return len(body_text.strip()), len(page_source)

    def _wait_for_document_owner(
        self, max_wait: float, previous_state: tuple[int, int] | None = None
    ) -> None:
        deadline = time.time() + max(0.0, max_wait)
        last_state = None
        stable_count = 0
        while True:
            state = self._get_document_state_owner()
            stable_count, ready = advance_wait_state(state, previous_state, last_state, stable_count)
            if ready or time.time() >= deadline:
                return
            last_state = state
            time.sleep(0.25)

    def get_documents(
        self,
        url: str,
        period: int,
        click_first: bool,
        timeout: int,
        wait_selector: str = "",
        wait_anchor: str = "",
    ) -> list[str]:
        if self._executor is None:
            raise RuntimeError("Browser is not started")
        return self._dispatch(
            lambda: self._get_documents_owner(
                url, period, click_first, timeout, wait_selector, wait_anchor
            )
        )

    def _wait_for_selector_period_owner(
        self,
        selector: str,
        anchor: str,
        period: int,
        max_wait: float,
    ) -> bool:
        if self._page is None or not selector:
            return False
        deadline = time.time() + max(0.0, max_wait)
        while True:
            try:
                texts = self._page.locator(selector).all_inner_texts()
            except Exception:
                texts = []
            if selector_period_ready(texts, period, anchor):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.25)

    def _get_documents_owner(
        self,
        url: str,
        period: int,
        click_first: bool,
        timeout: int,
        wait_selector: str = "",
        wait_anchor: str = "",
    ) -> list[str]:
        if self._page is None:
            raise RuntimeError("Browser is not started")
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        with suppress(PlaywrightTimeoutError):
            self._page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        self._wait_for_document_owner(min(float(timeout), 5.0))
        if wait_selector:
            self._wait_for_selector_period_owner(
                wait_selector,
                wait_anchor,
                period,
                min(float(timeout), 10.0),
            )
        self.capture_sequence = 0
        documents = self._collect_documents_owner(url)
        if click_first:
            previous_state = self._get_document_state_owner()
            if self._try_click_labels_owner():
                self._wait_for_document_owner(min(float(timeout), 2.0), previous_state)
                documents.extend(self._collect_documents_owner(url))
            for _ in range(2):
                previous_state = self._get_document_state_owner()
                if self._try_click_best_post_owner(period):
                    self._wait_for_document_owner(min(float(timeout), 3.0), previous_state)
                    documents.extend(self._collect_documents_owner(url))
        return documents

    def _collect_documents_owner(self, url: str = "") -> list[str]:
        if self._page is None:
            return []
        try:
            body_text = self._page.locator("body").inner_text()
        except Exception:
            body_text = ""
        state_id = f"browser:{url}:state:{self.capture_sequence}"
        self.capture_sequence += 1
        return [
            SourceDocument(
                body_text,
                source_url=url,
                fetch_kind="browser",
                document_type="body-text",
                parent_url=url,
                authority_id=f"{state_id}:body",
                document_id=f"{state_id}:body",
            ),
            SourceDocument(
                self._page.content(),
                source_url=url,
                fetch_kind="browser",
                document_type="page-source",
                parent_url=url,
                authority_id=f"{state_id}:page-source",
                document_id=f"{state_id}:page-source",
            ),
        ]

    def _try_click_labels_owner(self) -> bool:
        if self._page is None:
            return False
        script = r"""
        (labels) => {
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
        }
        """
        try:
            return bool(self._page.evaluate(script, ["高手论坛", "论坛", "帖子", "资料"]))
        except Exception:
            return False

    def _try_click_best_post_owner(self, period: int) -> bool:
        if self._page is None:
            return False
        script = r"""
        (period) => {
            const periodText = String(period);
            const keywordRe = /绝\s*杀\s*一\s*合|杀.{0,80}\d{1,2}\s*合/;
            let best = null;
            for (const el of document.querySelectorAll("a,article,li,section,div")) {
                const text = (el.innerText || "").replace(/\s+/g, " ").trim();
                if (!text || text.length < 6 || text.length > 900) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 30 || rect.height < 12) continue;
                let score = 0;
                if (text.includes(periodText + "期")) score += 60;
                if (keywordRe.test(text)) score += 80;
                if (/\d{1,2}\s*合/.test(text)) score += 20;
                if (/论坛|主页|首页|返回|登录|注册/.test(text)) score -= 30;
                if (score < 80) continue;
                const item = { el, score, top: rect.top };
                if (!best || item.score > best.score || (item.score === best.score && item.top < best.top)) best = item;
            }
            if (!best) return false;
            best.el.click();
            return true;
        }
        """
        try:
            return bool(self._page.evaluate(script, period))
        except Exception:
            return False


def close_browser_safely(browser: BrowserClient) -> None:
    with suppress(Exception):
        browser.close()


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
            except Exception:
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
        with suppress(Full):
            self.available.put_nowait(self.failure_token)

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
        client = self.available.get()
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
    browser_site_count = sum(1 for site in sites if site.browser)
    if browser_site_count == 0:
        return 0
    return min(3, max(1, workers), browser_site_count)
