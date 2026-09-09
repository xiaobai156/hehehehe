import os

CACHE_FILE = "he_success_cache.json"
SITES_FILE = "sites.json"
DEFAULT_DUPLICATE_FINGERPRINT_CACHE = "outputs/recent_10_cache.json"
DEFAULT_OUTPUT_DIR = os.getenv("HE_OUTPUT_DIR", r"C:\Users\Administrator\Desktop\每天工具\爬虫合集\七类数据统一归纳")
DEFAULT_FAILURE_OUTPUT_DIR = os.getenv("HE_FAILURE_OUTPUT_DIR", r"C:\Users\Administrator\Desktop\每天工具\爬虫合集\七类数据统一归纳失败")
