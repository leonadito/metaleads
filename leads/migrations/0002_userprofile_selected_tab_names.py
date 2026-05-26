from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('leads', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='selected_tab_names',
            field=models.JSONField(default=list),
        ),
    ]
