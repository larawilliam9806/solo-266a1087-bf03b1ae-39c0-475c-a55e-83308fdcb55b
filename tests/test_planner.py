from app.planner import (
    CalibFile,
    ImageSpec,
    build_plan,
    exposure_key,
    normalize_kind,
)


def F(i, kind="dark", det="D1", exp="60", flt="r", batch="B1", b=100, active=True):
    return CalibFile(
        id=i, kind=kind, detector=det, batch_id=batch, size_bytes=b,
        exposure_key=exposure_key(exp) if exp is not None else None,
        filter_name=flt, is_active=active,
    )


def I(i, det="D1", exp="60", flt="r"):
    return ImageSpec(image_id=i, detector=det, exposure_key=exposure_key(exp), filter_name=flt)


def test_exposure_canonicalisation():
    assert exposure_key(60) == exposure_key(60.0) == exposure_key("60.00") == "60"
    assert exposure_key("0.1") == exposure_key(0.10)
    assert exposure_key(None) is None


def test_kind_normalisation_chinese():
    assert normalize_kind("DARK") == "dark"
    assert normalize_kind("暗场") == "dark"
    assert normalize_kind("平场帧") == "flat"
    assert normalize_kind("Flat_Field".replace("_Field", "")) == "flat"


def test_simple_pair():
    plan = build_plan([I("a")], [F(1, "dark"), F(2, "flat")])
    assert plan.complete
    assert plan.total_bytes == 200
    assert [x.id for x in plan.selected_files] == [1, 2]
    item = plan.items[0]
    assert item.dark.id == 1 and item.flat.id == 2


def test_reuse_counts_once():
    files = [
        F(1, "dark", b=1000), F(2, "flat", b=1000, batch="B1"),
        F(3, "dark", b=1, batch="B2"), F(4, "flat", b=1, batch="B2"),
    ]
    plan = build_plan([I("a"), I("b"), I("c")], files)
    assert plan.complete
    assert plan.total_bytes == 2
    assert plan.selected_ids() == [3, 4]
    assert all(it.dark.id == 3 and it.flat.id == 4 for it in plan.items)


def test_same_batch_constraint_blocks_cross_pair():
    plan = build_plan([I("a")], [F(1, "dark", batch="B1"), F(2, "flat", batch="B2")])
    assert not plan.complete
    assert plan.missing[0]["image_id"] == "a"
    assert "dark_and_flat_not_same_batch" in plan.missing[0]["reasons"]


def test_detector_exposure_filter_constraints():
    # wrong detector
    p1 = build_plan([I("a", det="D2")], [F(1), F(2, "flat")])
    assert not p1.complete and "no_active_file_for_detector" in p1.missing[0]["reasons"]

    # wrong exposure
    p2 = build_plan([I("a", exp="30")], [F(1, exp="60"), F(2, "flat")])
    assert not p2.complete and "no_dark_with_same_exposure" in p2.missing[0]["reasons"]

    # wrong filter
    p3 = build_plan([I("a", flt="g")], [F(1), F(2, "flat", flt="r")])
    assert not p3.complete and "no_flat_with_same_filter" in p3.missing[0]["reasons"]


def test_inactive_files_ignored():
    plan = build_plan([I("a")], [F(1, active=False), F(2, "flat", active=False)])
    assert not plan.complete


def test_global_minimum_with_cross_image_sharing_tradeoff():
    # Shared batch covers both images with 4 files * 100 = 400 bytes.
    # Separate cheap batches cost 1+1 + 1+1 = 4 bytes -> global optimum is 4.
    files = [
        F(1, "dark", exp="60", flt=None, batch="BS", b=100),
        F(2, "dark", exp="120", flt=None, batch="BS", b=100),
        F(3, "flat", exp=None, flt="r", batch="BS", b=100),
        F(4, "flat", exp=None, flt="g", batch="BS", b=100),
        F(5, "dark", exp="60", flt=None, batch="B1", b=1),
        F(6, "flat", exp=None, flt="r", batch="B1", b=1),
        F(7, "dark", exp="120", flt=None, batch="B2", b=1),
        F(8, "flat", exp=None, flt="g", batch="B2", b=1),
    ]
    images = [I("i1", exp="60", flt="r"), I("i2", exp="120", flt="g")]
    plan = build_plan(images, files)
    assert plan.complete and plan.total_bytes == 4


def test_partial_order_lists_all_unmatchable_images():
    files = [F(1, "dark"), F(2, "flat")]
    images = [I("ok"), I("bad-exp", exp="999"), I("bad-det", det="X")]
    plan = build_plan(images, files)
    assert not plan.complete
    assert [m["image_id"] for m in plan.missing] == ["bad-exp", "bad-det"]


# ---------------------------------------------------------------------------
# Property comparison against an independent, obviously-correct optimiser.
# ---------------------------------------------------------------------------

def _naive_best(images, files):
    import itertools

    choices_per_image = []
    for img in images:
        darks = [f for f in files if f.is_active and f.kind == "dark"
                 and f.detector == img.detector and f.exposure_key == img.exposure_key]
        flats = [f for f in files if f.is_active and f.kind == "flat"
                 and f.detector == img.detector and f.filter_name == img.filter_name]
        pairs = [(d, fl) for d in darks for fl in flats if d.batch_id == fl.batch_id]
        if not pairs:
            return None
        choices_per_image.append(pairs)

    best = None
    for combo in itertools.product(*choices_per_image):
        union = {}
        for d, fl in combo:
            union[d.id] = d.size_bytes
            union[fl.id] = fl.size_bytes
        cost = sum(union.values())
        if best is None or cost < best:
            best = cost
    return best


def test_random_instances_match_naive_optimum():
    import random

    rng = random.Random(20261125)
    detectors = ["D1", "D2"]
    exposures = ["30", "60", "120"]
    filters = ["r", "g"]
    batches = ["B1", "B2", "B3"]

    for trial in range(300):
        m = rng.randint(1, 9)
        files = []
        for fid in range(1, m + 1):
            kind = rng.choice(["dark", "flat"])
            files.append(CalibFile(
                id=fid,
                kind=kind,
                detector=rng.choice(detectors),
                batch_id=rng.choice(batches),
                size_bytes=rng.randint(0, 500),
                exposure_key=exposure_key(rng.choice(exposures)) if kind == "dark" else None,
                filter_name=rng.choice(filters) if kind == "flat" else None,
                is_active=rng.random() > 0.2,
            ))
        n = rng.randint(1, 4)
        images = [
            ImageSpec(f"img{i}", rng.choice(detectors),
                      exposure_key(rng.choice(exposures)), rng.choice(filters))
            for i in range(n)
        ]
        plan = build_plan(images, files)
        naive = _naive_best(images, files)
        if naive is None:
            assert not plan.complete, trial
        else:
            assert plan.complete, trial
            assert plan.total_bytes == naive, (trial, plan.total_bytes, naive)
