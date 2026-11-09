from . import catalog
from .models import Blueprint, Expansion
from .recognition import Recognition, match
from .tests import LedgerTestCase

FABLE = "Fable of the Mirror-Breaker"


class MatchBase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.api = self.ct.client()
        catalog.sync_expansions(self.api)
        self.neo = Expansion.objects.get(code="neo")
        catalog.ensure_blueprints(self.api, self.neo)


class MatchTest(MatchBase):
    def test_number_in_locked_expansion(self):
        m = match(self.api, Recognition(name=FABLE, collector_number="0356", language="IT"), self.neo)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 1101))

    def test_name_only_with_versions_is_ambiguous(self):
        m = match(self.api, Recognition(name=FABLE, language="EN"), self.neo)
        self.assertEqual(m.reason, "ambiguous")
        self.assertIsNone(m.blueprint)
        self.assertEqual([b.ct_id for b in m.candidates], [1100, 1101])

    def test_all_expansions_lazy_loads_set(self):
        rec = Recognition(name="Sheoldred, the Apocalypse", set_code="dmu", collector_number="107", language="EN")
        m = match(self.api, rec, None)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 1301))
        self.assertTrue(Blueprint.objects.filter(expansion__code="dmu").exists())

    def test_unknown_set_code_not_found(self):
        m = match(self.api, Recognition(name="Nothing", set_code="zzz", language="EN"), None)
        self.assertEqual((m.reason, m.blueprint, m.candidates), ("not_found", None, []))

    def test_name_number_mismatch_is_ambiguous(self):
        m = match(self.api, Recognition(name=FABLE, collector_number="999", language="EN"), self.neo)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual({b.ct_id for b in m.candidates}, {1100, 1101})

    def test_split_card_front_face_matches(self):
        Blueprint.objects.create(ct_id=1199, expansion=self.neo, name="Fire // Ice", collector_number="400")
        m = match(self.api, Recognition(name="Fire", language="EN"), self.neo)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 1199))

    def test_unreadable_language_needs_confirmation(self):
        m = match(self.api, Recognition(name=FABLE, collector_number="356", language=""), self.neo)
        self.assertEqual(m.reason, "language")
        self.assertIsNone(m.blueprint)
        self.assertEqual([b.ct_id for b in m.candidates], [1101])


class MatchReviewFixesTest(MatchBase):
    def test_locked_expansion_follows_other_printed_set(self):  # I2
        rec = Recognition(name="Counterspell", set_code="dmr", collector_number="45", language="EN")
        m = match(self.api, rec, self.neo)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 1201))

    def test_name_only_across_loaded_sets_is_uncertain(self):  # I3
        m = match(self.api, Recognition(name=FABLE + "", collector_number="356", language="EN"), None)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual([b.ct_id for b in m.candidates], [1101])

    def test_unknown_codes_sync_expansions_once(self):  # I5
        match(self.api, Recognition(name="X", set_code="zz1", language="EN"), None)
        match(self.api, Recognition(name="Y", set_code="zz2", language="EN"), None)
        self.assertEqual(self.ct.calls.count(("GET", "/expansions")), 2)  # setUp + one throttled sync


class PromoNumberMismatchTest(MatchBase):
    """Issue #14: a promo read as PRM #1 must not be offered as another card numbered 1."""

    def setUp(self):
        super().setUp()
        from django.utils import timezone

        self.prm = Expansion.objects.create(ct_id=500, code="prm", name="30th Anniversary Promos",
                                            blueprints_synced_at=timezone.now())
        for ct_id, name, version in [(268473, "Destroy Evil", ""), (219682, "Arcane Signet", "Festival Promo"),
                                     (262181, "Lotus Petal", "WPN Promo"),
                                     (219684, "Richard Garfield, Ph.D.", "Magic 30 Promo")]:
            Blueprint.objects.create(ct_id=ct_id, expansion=self.prm, name=name, version=version,
                                     collector_number="1")
        self.mrd = Expansion.objects.create(ct_id=501, code="mrd", name="Mirrodin",
                                            blueprints_synced_at=timezone.now())
        Blueprint.objects.create(ct_id=5010, expansion=self.mrd, name="Gilded Lotus", collector_number="202")

    def rec(self, name="Gilded Lotus"):
        return Recognition(name=name, set_code="PRM", collector_number="1", language="EN", foil=True)

    def test_name_found_outside_the_scope_comes_first(self):
        m = match(self.api, self.rec(), None)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual(m.candidates[0].ct_id, 5010)

    def test_every_number_hit_is_considered(self):
        m = match(self.api, self.rec("Lotus Petal"), None)
        self.assertEqual((m.reason, m.blueprint.ct_id), ("ok", 262181))

    def test_number_hits_follow_name_matches(self):
        m = match(self.api, self.rec(), None)
        self.assertEqual([b.ct_id for b in m.candidates], [5010, 268473, 219682, 219684, 262181])

    def test_name_in_scope_hides_other_expansions(self):
        Blueprint.objects.create(ct_id=268999, expansion=self.prm, name="Gilded Lotus", version="Promo",
                                 collector_number="P1")
        m = match(self.api, self.rec(), None)
        self.assertEqual([b.ct_id for b in m.candidates][:1], [268999])
        self.assertNotIn(5010, [b.ct_id for b in m.candidates])

    def test_name_only_falls_back_outside_the_scope(self):
        rec = Recognition(name="Gilded Lotus", set_code="PRM", language="EN")
        m = match(self.api, rec, None)
        self.assertEqual((m.reason, [b.ct_id for b in m.candidates]), ("ambiguous", [5010]))

    def test_unknown_name_offers_number_hits(self):
        m = match(self.api, self.rec("Nothing Like It"), None)
        self.assertEqual(m.reason, "ambiguous")
        self.assertEqual(len(m.candidates), 4)
