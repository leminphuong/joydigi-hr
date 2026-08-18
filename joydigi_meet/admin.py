from django.contrib import admin

from joydigi_meet import models

# Register your models here.
admin.site.register(models.GoogleCredential)
admin.site.register(models.GoogleCloudCredential)
admin.site.register(models.GoogleMeeting)
