# Generated by Django 4.1.7 on 2023-03-28 23:01

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "subscription",
            "0004_rename_description_filingwebhookevent_document_description",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="filingwebhookevent",
            name="doc_id",
            field=models.IntegerField(
                help_text="The document id from CL.", null=True
            ),
        ),
    ]
