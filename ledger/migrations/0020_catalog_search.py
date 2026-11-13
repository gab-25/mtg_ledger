from django.contrib.postgres.operations import TrigramExtension, UnaccentExtension
from django.db import migrations

# Lower case, no accents, no apostrophes, every other run of non-alphanumerics one space: the form the catalog search
# matches query words against. IMMUTABLE (unaccent itself is only STABLE) so it can back an index.
FUNCTION = r"""
CREATE FUNCTION ledger_search_text(text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
  SELECT btrim(regexp_replace(regexp_replace(
    replace(lower(public.unaccent('public.unaccent'::regdictionary, $1)), 'æ', 'ae'),
    '[''’]', '', 'g'), '[^a-z0-9]+', ' ', 'g'))
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("ledger", "0019_card_is_product")]

    operations = [
        TrigramExtension(),
        UnaccentExtension(),
        migrations.RunSQL(FUNCTION, "DROP FUNCTION ledger_search_text(text);"),
        migrations.RunSQL(
            "CREATE INDEX ledger_blueprint_search_trgm ON ledger_blueprint "
            "USING gin (ledger_search_text(name) gin_trgm_ops);",
            "DROP INDEX ledger_blueprint_search_trgm;",
        ),
    ]
