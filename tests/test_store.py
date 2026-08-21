from vera_bot.store import ContextStore


def test_idempotent_on_version():
    store = ContextStore()
    payload = {"merchant_id": "m1", "performance": {"views": 100}}
    assert store.put("merchant", "m1", 1, payload)[:2] == (True, 1)
    assert store.put("merchant", "m1", 1, payload)[:2] == (False, 1)
    assert store.put("merchant", "m1", 0, payload)[:2] == (False, 1)


def test_higher_version_replaces_and_diffs():
    store = ContextStore()
    store.put("merchant", "m1", 1, {"performance": {"views": 2410, "calls": 18},
                                    "signals": ["a"], "subscription": {"status": "active"}})
    accepted, version, change = store.put(
        "merchant", "m1", 2, {"performance": {"views": 2580, "calls": 31},
                              "signals": ["a", "b"], "subscription": {"status": "expired"}})
    assert (accepted, version) == (True, 2)
    assert change.metrics["profile views"] == (2410, 2580)
    assert change.added_signals == ["b"]
    assert change.state_changes["subscription.status"] == ("active", "expired")
    assert store.get("merchant", "m1")["performance"]["views"] == 2580


def test_category_inferred_from_merchant_id_when_slug_missing():
    store = ContextStore()
    store.put("category", "dentists", 1, {"slug": "dentists"})
    merchant = {"merchant_id": "m_011_dr_sameer_dentist_bangalore"}
    assert store.category_for_merchant(merchant)["slug"] == "dentists"


def test_counts_and_live_triggers():
    store = ContextStore()
    store.put("trigger", "t1", 1, {"id": "t1", "expires_at": "2020-01-01T00:00:00Z"})
    store.put("trigger", "t2", 1, {"id": "t2", "expires_at": "2099-01-01T00:00:00Z"})
    assert store.counts()["trigger"] == 2
    assert store.live_trigger_ids("2026-04-26T00:00:00Z") == ["t2"]
