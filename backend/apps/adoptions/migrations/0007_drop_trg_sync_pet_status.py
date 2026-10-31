# Generated for Milestone 5 Trigger Convergence

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('adoptions', '0006_adoptionapplication_has_fenced_yard_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DROP TRIGGER IF EXISTS trg_sync_pet_status ON adoption_applications;
            DROP FUNCTION IF EXISTS sync_pet_status_on_application_change();
            """,
            reverse_sql=migrations.RunSQL.noop
        ),
    ]
