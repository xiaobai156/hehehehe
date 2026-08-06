from he_app.domain.errors import DedicatedCandidateConflict
from he_app.domain.models import Candidate
from he_app.domain.policies import normalize_pick
from he_app.validation.period import is_valid_sum_value


def select_unique_candidate(
    candidates: list[Candidate],
    pick: str,
    detect_conflict: bool = True,
) -> Candidate | None:
    if not candidates:
        return None

    if detect_conflict:
        values_by_line: list[tuple[str, str]] = []
        seen_lines: set[str] = set()
        for candidate in candidates:
            if candidate.line in seen_lines:
                continue
            seen_lines.add(candidate.line)
            if is_valid_sum_value(candidate.values):
                values_by_line.append((candidate.values, candidate.line))
        unique_values = sorted({value for value, _line in values_by_line}, key=lambda value: int(value[:2]))
        if len(unique_values) > 1:
            raise DedicatedCandidateConflict(unique_values, [line for _value, line in values_by_line])

    return candidates[0] if normalize_pick(pick) == "top" else candidates[-1]

