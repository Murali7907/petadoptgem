from django.db import migrations

TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION check_review_eligibility()
RETURNS TRIGGER AS $$
DECLARE
    delivery_status_val VARCHAR;
BEGIN
    SELECT status INTO delivery_status_val FROM pet_deliveries WHERE application_id = NEW.application_id;
    IF delivery_status_val IS NULL OR delivery_status_val <> 'delivered' THEN
        RAISE EXCEPTION 'Reviews can only be submitted for completed deliveries.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_review_eligibility ON adoption_reviews;
CREATE TRIGGER trg_check_review_eligibility
BEFORE INSERT OR UPDATE ON adoption_reviews
FOR EACH ROW
EXECUTE FUNCTION check_review_eligibility();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS trg_check_review_eligibility ON adoption_reviews;
DROP FUNCTION IF EXISTS check_review_eligibility();
"""

class Migration(migrations.Migration):

    dependencies = [
        ('adoptions', '0003_adoptionreview_driver_rating_and_more'),
        ('logistics', '0003_payment_amount_in_paise_payment_currency_and_more'),
    ]

    operations = [
        migrations.RunSQL(TRIGGER_SQL, reverse_sql=REVERSE_SQL),
    ]
