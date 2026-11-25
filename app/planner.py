"""Calibration matching and minimum-download planning.

Rules implemented here
----------------------
For every raw image:

* one **dark** frame with the same detector and the same exposure time;
* one **flat** frame with the same detector and the same filter;
* the chosen dark and flat must belong to the same calibration batch;
* only active ("still in service") files may be selected.

An order references at most ``MAX_CANDIDATES`` calibration files.  Because the
candidate set is tiny (<= 16), the global optimum is found by exhaustive
enumeration of all ``2**m`` subsets: a subset is feasible when every image has
at least one valid dark+flat pair fully contained in it.  The feasible subset
with the smallest sum of distinct file sizes is optimal.  Reusing a file
across images therefore costs its bytes only once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Iterable

MAX_CANDIDATES = 16


def normalize_kind(value: Any) -> str | None:
    """Return ``"dark"`` / ``"flat"`` for the many spellings used by clients."""
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if text in {"dark", "darks", "darkframe", "darkframes", "darkcurrent", "暗场", "暗场帧", "暗帧", "暗电流"}:
        return "dark"
    if text in {"flat", "flats", "flatframe", "flatframes", "平场", "平场帧"}:
        return "flat"
    # Chinese tokens survive lower()/replace; do a direct containment check.
    raw = str(value).strip()
    if "暗" in raw:
        return "dark"
    if "平" in raw:
        return "flat"
    return text or None


def exposure_key(value: Any) -> str | None:
    """Canonical key so ``60``, ``60.0`` and ``"60.00"`` compare equal."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
    else:
        text = str(value)
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if dec == 0:
        return "0"
    return format(dec.normalize(), "f")


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True)
class CalibFile:
    id: int
    kind: str
    detector: str
    batch_id: str
    size_bytes: int
    exposure_key: str | None = None
    filter_name: str | None = None
    is_active: bool = True
    code: str | None = None
    exposure_seconds: float | int | str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @staticmethod
    def from_row(row: Any) -> "CalibFile":
        data = dict(row)
        exp_raw = data.get("exposure_value")
        return CalibFile(
            id=int(data["id"]),
            code=data.get("code"),
            kind=normalize_kind(data.get("kind")) or str(data.get("kind")),
            detector=data.get("detector") or "",
            batch_id=data.get("batch_id") or "",
            size_bytes=int(data.get("size_bytes") or 0),
            exposure_key=data.get("exposure_key") or exposure_key(exp_raw),
            filter_name=data.get("filter_name"),
            is_active=bool(data.get("is_active", 1)),
            exposure_seconds=exp_raw,
            raw=data,
        )


@dataclass(frozen=True)
class ImageSpec:
    image_id: str
    detector: str
    exposure_key: str | None
    filter_name: str | None
    exposure_seconds: Any = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class AssignedPair:
    image: ImageSpec
    dark: CalibFile
    flat: CalibFile

    @property
    def mask(self) -> int:  # pragma: no cover - trivial
        return 0


@dataclass
class ImageMatch:
    image: ImageSpec
    pair_masks: list[int]
    pair_choices: list[tuple[CalibFile, CalibFile]]
    reasons: list[str]


@dataclass
class PlanResult:
    complete: bool
    items: list[AssignedPair]
    missing: list[dict[str, Any]]
    selected_files: list[CalibFile]
    total_bytes: int | None
    candidate_count: int
    active_count: int

    def selected_ids(self) -> list[int]:
        return [f.id for f in self.selected_files]


def _pair_candidates(
    image: ImageSpec, active: list[CalibFile], index: dict[int, int]
) -> tuple[list[tuple[CalibFile, CalibFile]], list[str]]:
    reasons: list[str] = []

    same_detector = [f for f in active if norm_text(f.detector) == norm_text(image.detector)]
    if not same_detector:
        reasons.append("no_active_file_for_detector")

    any_dark_detector = [f for f in same_detector if f.kind == "dark"]
    if image.exposure_key is not None:
        darks = [f for f in any_dark_detector if f.exposure_key == image.exposure_key]
        if not darks and any_dark_detector:
            reasons.append("no_dark_with_same_exposure")
        elif not any_dark_detector:
            reasons.append("no_dark_for_detector")
    else:
        darks = list(any_dark_detector)
        if not darks:
            reasons.append("image_missing_exposure_and_no_dark")

    any_flat_detector = [f for f in same_detector if f.kind == "flat"]
    if image.filter_name:
        target_filter = norm_text(image.filter_name)
        flats = [f for f in any_flat_detector if norm_text(f.filter_name) == target_filter]
        if not flats and any_flat_detector:
            reasons.append("no_flat_with_same_filter")
        elif not any_flat_detector:
            reasons.append("no_flat_for_detector")
    else:
        flats = list(any_flat_detector)
        if not flats:
            reasons.append("image_missing_filter_and_no_flat")

    pairs = [(d, fl) for d in darks for fl in flats if norm_text(d.batch_id) == norm_text(fl.batch_id)]
    if darks and flats and not pairs:
        reasons.append("dark_and_flat_not_same_batch")
    if not pairs and not reasons:
        reasons.append("no_valid_dark_flat_pair")
    return pairs, reasons


@lru_cache(maxsize=32)
def _zero_bit_masks(m: int) -> tuple[int, ...]:
    """Masks whose bit *position* j is 0, for j in 0..m-1.

    Position k represents subset mask k.  Mask j therefore selects exactly the
    subset indices whose j-th element bit is zero; used by the SOS dilation.
    """
    n_positions = 1 << m  # number of subsets
    all_bits = (1 << n_positions) - 1
    masks: list[int] = []
    for j in range(m):
        half = 1 << j                    # low block length (in bit positions)
        low_ones = (1 << half) - 1
        block = half << 1                # period length = 2 * half
        # low_ones repeated every ``block`` positions across n_positions bits
        repetitions = all_bits // ((1 << block) - 1)
        masks.append(low_ones * repetitions)
    return tuple(masks)


def _good_subsets_bitset(pair_masks: list[int], m: int, zero_masks: tuple[int, ...]) -> int:
    """Return a bitset whose bit S is set iff subset S contains a valid pair."""
    good = 0
    for pm in pair_masks:
        good |= 1 << pm
    for j in range(m):
        good |= (good & zero_masks[j]) << (1 << j)
    return good


def _optimal_subset(
    matches: list[ImageMatch], files: list[CalibFile]
) -> tuple[int, list[AssignedPair]] | None:
    """Exhaustive 2**m search; return (mask, assignments) or ``None``.

    Feasibility is evaluated with an O(m * 2**m)-style SOS closure compressed
    into Python big integers, deduplicated by pair-mask signature, so hundreds
    of images do not multiply the 2**16 enumeration cost.
    """
    m = len(files)
    subset_count = 1 << m
    weights = [max(0, int(f.size_bytes or 0)) for f in files]

    subset_cost = [0] * subset_count
    for mask in range(1, subset_count):
        bit = mask & -mask
        j = bit.bit_length() - 1
        subset_cost[mask] = subset_cost[mask ^ bit] + weights[j]

    # Intersect the feasible-subset bitsets of every *distinct* signature.
    zero_masks = _zero_bit_masks(m)
    feasible = (1 << subset_count) - 1
    seen_signatures: set[tuple[int, ...]] = set()
    for match in matches:
        signature = tuple(match.pair_masks)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        feasible &= _good_subsets_bitset(match.pair_masks, m, zero_masks)
        if feasible == 0:
            return None

    feasible_bytes = feasible.to_bytes((subset_count + 7) // 8, "little")

    best_key: tuple[int, int, tuple[int, ...]] | None = None
    best_mask: int | None = None

    for mask in range(subset_count):
        if not ((feasible_bytes[mask >> 3] >> (mask & 7)) & 1):
            continue
        id_tuple = tuple(sorted(files[j].id for j in range(m) if mask & (1 << j)))
        key = (subset_cost[mask], mask.bit_count(), id_tuple)
        if best_key is None or key < best_key:
            best_key = key
            best_mask = mask

    if best_mask is None or best_key is None:
        return None

    # Deterministic assignment inside the optimal subset.
    assignments: list[AssignedPair] = []
    for match in matches:
        choices = [
            (d, fl)
            for (d, fl), pm in zip(match.pair_choices, match.pair_masks)
            if pm & ~best_mask == 0
        ]
        d, fl = min(choices, key=lambda df: (df[0].id, df[1].id))
        assignments.append(AssignedPair(image=match.image, dark=d, flat=fl))

    return best_mask, assignments


def build_plan(images: Iterable[ImageSpec], candidates: Iterable[CalibFile]) -> PlanResult:
    """Compute the minimum-download plan for one order.

    Inactive candidates are ignored.  If one or more images have no legal
    pair, the order is incomplete: those images are listed in ``missing`` and
    no publishable total is claimed (a best-effort partial plan is still
    returned via the service layer).
    """
    image_list = list(images)
    all_candidates = list(candidates)
    active = [f for f in all_candidates if f.is_active]
    index = {f.id: i for i, f in enumerate(active)}

    matches: list[ImageMatch] = []
    missing: list[dict[str, Any]] = []

    for image in image_list:
        choices, reasons = _pair_candidates(image, active, index)
        pair_masks: list[int] = []
        for d, fl in choices:
            pair_masks.append((1 << index[d.id]) | (1 << index[fl.id]))
        match = ImageMatch(
            image=image,
            pair_masks=pair_masks,
            pair_choices=choices,
            reasons=sorted(set(reasons)),
        )
        matches.append(match)
        if not choices:
            missing.append(
                {
                    "image_id": image.image_id,
                    "reasons": match.reasons,
                    "image": image.raw or {
                        "image_id": image.image_id,
                        "detector": image.detector,
                        "exposure_seconds": image.exposure_seconds,
                        "filter": image.filter_name,
                    },
                }
            )

    feasible_matches = [m for m in matches if m.pair_masks]

    if missing:
        # Best-effort optimisation for the images that *can* be paired; the
        # order itself stays unpublished.
        partial = _optimal_subset(feasible_matches, active) if feasible_matches else None
        if partial is not None:
            mask, assignments = partial
            selected = [active[j] for j in range(len(active)) if mask & (1 << j)]
            total: int | None = sum(f.size_bytes for f in selected)
        else:
            assignments, selected, total = [], [], None
        return PlanResult(
            complete=False,
            items=assignments,
            missing=missing,
            selected_files=selected,
            total_bytes=total,
            candidate_count=len(all_candidates),
            active_count=len(active),
        )

    optimal = _optimal_subset(matches, active)
    # With no missing images an optimum must exist; guard anyway.
    if optimal is None:  # pragma: no cover - defensive
        return PlanResult(False, [], missing, [], None, len(all_candidates), len(active))

    mask, assignments = optimal
    selected = [active[j] for j in range(len(active)) if mask & (1 << j)]
    total = sum(f.size_bytes for f in selected)
    return PlanResult(
        complete=True,
        items=assignments,
        missing=[],
        selected_files=selected,
        total_bytes=total,
        candidate_count=len(all_candidates),
        active_count=len(active),
    )
