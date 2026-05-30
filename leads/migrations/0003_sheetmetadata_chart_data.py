from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('leads', '0002_userprofile_selected_tab_names'),
    ]

    operations = [
        migrations.AddField(
            model_name='sheetmetadata',
            name='chart_data',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
