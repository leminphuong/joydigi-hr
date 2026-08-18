"""
joydigi/joydigi_backends_gcp.py
"""

from django.db import models
from storages.backends.gcloud import GoogleCloudStorage

from joydigi import settings


class PrivateMediaStorage(GoogleCloudStorage):
    """
    PrivateMediaStorage
    """

    location = settings.env("NAMESPACE", default="private")
    default_acl = "private"
    file_overwrite = False


# To set the private storage globally
models.FileField.storage = PrivateMediaStorage()
models.ImageField.storage = PrivateMediaStorage()
