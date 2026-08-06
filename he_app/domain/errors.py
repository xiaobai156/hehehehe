class SiteScrapeFailure(Exception):
    def __init__(self, category: str, reason: str):
        super().__init__(reason)
        self.category = category
        self.reason = reason


class DedicatedCandidateConflict(Exception):
    def __init__(self, values: list[str], lines: list[str]):
        super().__init__("/".join(values))
        self.values = values
        self.lines = lines


class SiteConfigError(RuntimeError):
    pass


class FingerprintCacheError(RuntimeError):
    pass

