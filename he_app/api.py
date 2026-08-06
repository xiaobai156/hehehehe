"""Stable public API shared by the three command-line entry points."""

# ruff: noqa: F401,F403

from he_app.config.settings import (
    CACHE_FILE,
    DEFAULT_DUPLICATE_FINGERPRINT_CACHE,
    DEFAULT_FAILURE_OUTPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    SITES_FILE,
)
from he_app.config.sites import load_sites, write_sites_config
from he_app.domain.errors import DedicatedCandidateConflict, FingerprintCacheError, SiteConfigError, SiteScrapeFailure
from he_app.domain.models import Candidate, FailureInfo, PreviousInfo, Site, SiteRule
from he_app.domain.policies import normalize_digit_text, normalize_pick, normalize_text
from he_app.fetch.browser import BrowserClient, BrowserPool, default_browser_pool_size
from he_app.fetch.discovery import collect_documents, collect_documents_from_page, collect_page_documents
from he_app.fetch.http import build_host_locks, create_session, fetch_text, host_key
from he_app.observability.progress import *
from he_app.parsers.common import *
from he_app.parsers.dedicated.history import *
from he_app.parsers.dedicated.history import BATCH_NEW_DEDICATED_SITE_IDS, DEDICATED_SITE_IDS
from he_app.parsers.dedicated.structured import *
from he_app.parsers.dedicated.structured import DAJIAFA_SITE_ID, YIAIZHIMING_SITE_ID
from he_app.parsers.dedicated.tables import *
from he_app.services.crawler import *
from he_app.services.document_sources import *
from he_app.services.runner import build_arg_parser
from he_app.services.single_period import evaluate_site_period as evaluate_site_documents
from he_app.services.single_period import parse_site_period
from he_app.storage.atomic_write import exclusive_path_lock, write_text_atomic
from he_app.storage.atomic_write import write_text_atomic_unlocked as _write_text_atomic_unlocked
from he_app.storage.recent_cache import (
    load_recent_cache as load_duplicate_fingerprint_cache,
    trim_fingerprint as trim_duplicate_fingerprint,
    update_recent_cache_from_outcomes as update_duplicate_fingerprint_cache_from_outcomes,
)
from he_app.storage.reports import *
from he_app.storage.success_cache import *
