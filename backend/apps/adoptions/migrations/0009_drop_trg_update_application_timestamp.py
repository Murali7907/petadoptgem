# Generated for Milestone 8a Trigger Drift Elimination

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('adoptions', '0008_alter_adoptionapplication_options_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DROP TRIGGER IF EXISTS trg_update_application_timestamp ON adoption_applications;
            DROP FUNCTION IF EXISTS update_updated_at_column();
            """,
            reverse_sql=migrations.RunSQL.noop
        ),
    ]
