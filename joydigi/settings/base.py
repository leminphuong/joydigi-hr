"""
base.py — Main Django settings for Joydigi
"""

import os
from datetime import timedelta
from os.path import join
from pathlib import Path

import environ
from django.contrib.messages import constants as messages
from django.core.files.storage import FileSystemStorage

# ========================================
# BASE PATH & ENVIRONMENT CONFIGURATION
# ========================================
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, "django-insecure-default-key"),
    ALLOWED_HOSTS=(list, ["*"]),
    CSRF_TRUSTED_ORIGINS=(list, ["http://localhost:8000"]),
    SECURE_SSL_REDIRECT=(bool, False),
)

# Existing process environment (Compose, systemd, CI) wins over .env values.
env.read_env(os.path.join(BASE_DIR, ".env"), overwrite=False)

# ========================================
# CORE DJANGO SETTINGS
# ========================================
SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
JOYDIGI_ENV = env("JOYDIGI_ENV", default="")
REDIS_URL = env("REDIS_URL", default=None)

# Firebase Cloud Messaging, used only to push the end-of-day check-out
# reminders. Points at a service-account JSON file that is deliberately
# NOT in the repository: set it in the deployment environment. Left unset,
# push is simply skipped and the in-app notification still works — a
# developer machine without the secret must never fail to start.
FIREBASE_CREDENTIALS_FILE = env("FIREBASE_CREDENTIALS_FILE", default="")

# Phase NOTIFY-2. Who owns the APScheduler that drives auto punch-out,
# forgotten-session finalization, work records and the four attendance
# reminders.
#
#   embedded  — every web process starts its own (the behaviour this
#               project has always had, and the default, so a deploy that
#               has not been reconfigured keeps working exactly as before)
#   dedicated — web processes start none; one separate process owns it,
#               started with `manage.py run_attendance_scheduler`
#   disabled  — nothing starts one anywhere
#
# `embedded` is honest rather than good: under `gunicorn --workers 3`
# there are three schedulers on the same one-minute tick, and the only
# thing stopping a duplicate reminder is the stored notification each run
# checks for first. `dedicated` is the target, and reaching it needs a
# server-side unit that does not live in this repository — see the phase
# report. The default stays `embedded` precisely so that shipping this
# code cannot silently stop auto punch-out.
ATTENDANCE_SCHEDULER_MODE = env("ATTENDANCE_SCHEDULER_MODE", default="embedded")

# In-process 1:1 face recognition. The model is loaded once per Django process.
FACE_VERIFY_THRESHOLD = env.float("FACE_VERIFY_THRESHOLD", default=0.55)
FACE_MODEL_NAME = env("FACE_MODEL_NAME", default="buffalo_l")
FACE_MODEL_ROOT = env("FACE_MODEL_ROOT", default="~/.insightface")
FACE_DETECTION_SIZE = env.int("FACE_DETECTION_SIZE", default=640)
FACE_IMAGE_MAX_BYTES = env.int("FACE_IMAGE_MAX_BYTES", default=5 * 1024 * 1024)

# Phase FIX A.1B — the first attendance date governed by automatic
# forgotten-session finalization, as YYYY-MM-DD.
#
# Sessions dated before this are historical: they predate the policy, no
# employee was ever told the system would close their day for them, and
# some of them are the very rows the incident left behind. They stay
# exactly as they are, for an administrator to review, and FIX A already
# guarantees they cannot block a new workday.
#
# Unset is the safe state and the default: with no cutoff, nothing is
# finalized automatically at all. That way a deployment that forgets this
# value quietly does less rather than quietly rewriting history — and the
# same applies to a value that will not parse.
#
# This governs *only* forgotten-session finalization. The configurable
# Auto Check Out feature (`EmployeeShiftSchedule.is_auto_punch_out_enabled`
# plus `auto_punch_out_time`) is a separate thing an administrator opts
# into per shift, and it is not affected by this setting.
ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF = env(
    "ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF", default=""
)

# Default site ID for django.contrib.sites framework.
SITE_ID = 1

THEME_APP = "joydigi_theme"

INSTALLED_APPS = [
    # Default Django apps
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # Third-party apps
    "notifications",
    "mathfilters",
    "corsheaders",
    "simple_history",
    "django_filters",
    "widget_tweaks",
    "auditlog",
    "django_apscheduler",
    "rest_framework",
    "rest_framework_simplejwt",
    # Core Joydigi apps
    "joydigi_auth",
    THEME_APP,
    "base",
    "employee",
    "leave",
    "attendance",
    "accessibility",
    "joydigi_audit",
    "joydigi_widgets",
    "joydigi_crumbs",
    # Kept as a shared data dependency for existing employee records. The
    # document-management routes and UI are disabled.
    "joydigi_documents",
    "joydigi_views",
    "joydigi_api",
    "pg_backup",
    "joydigi_dbtemplate",
]

# ========================================
# REST FRAMEWORK CONFIGURATION
# ========================================

REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        # Phase AUTH-6A.2: adds the session_version revocation check on
        # top of stock JWTAuthentication — see joydigi_api.auth for why
        # this is safe to change globally (the only real, mounted DRF
        # surface in this project is joydigi_api; `geofencing`, the
        # other app with DRF views, isn't in INSTALLED_APPS/urls at
        # all, and the web/admin UI authenticates via Django session
        # cookies, never this setting).
        "joydigi_api.auth.SessionVersionJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "PAGE_SIZE": 20,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    # Phase AUTH-6B: bounded but long — the mobile app is expected to
    # silently refresh well before this, and every successful refresh
    # mints a brand-new one (see TokenRefreshAPIView), so an
    # employee who opens the app at least once every 30 days never
    # sees Login just from time passing. Never literally infinite;
    # session_version (not this lifetime) is what makes admin
    # force-logout and single-device login actually immediate.
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
}

APSCHEDULER_DATETIME_FORMAT = "N j, Y, f:s a"

APSCHEDULER_RUN_NOW_TIMEOUT = 25  # Seconds

# ========================================
# MIDDLEWARE
# ========================================
MIDDLEWARE = [
    # Phase GLOBAL-ADMIN-PERF-B — TEMPORARY request timing. Registered
    # twice on purpose: outermost to own the measurement and write the
    # header, innermost to sit against the view. Remove both entries
    # together with `joydigi/perf_timing.py`.
    "joydigi.perf_timing.PerfTimingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Joydigi-specific middlewares
    "base.middleware.CompanyMiddleware",
    "joydigi_audit.middleware.UserActivityLogMiddleware",
    "base.middleware.ForcePasswordChangeMiddleware",
    "base.middleware.TwoFactorAuthMiddleware",
    "accessibility.middlewares.AccessibilityMiddleware",
    "joydigi.joydigi_middlewares.MethodNotAllowedMiddleware",
    "joydigi.joydigi_middlewares.SVGSecurityMiddleware",
    "joydigi.joydigi_middlewares.MissingParameterMiddleware",
    "auditlog.middleware.AuditlogMiddleware",
    # Phase GLOBAL-ADMIN-PERF-B — TEMPORARY, the inner half. See above.
    "joydigi.perf_timing.PerfTimingMiddleware",
]

ROOT_URLCONF = "joydigi.urls"

# ========================================
# DATABASE CONFIGURATION
# ========================================
if env("DATABASE_URL", default=None):
    DATABASES = {"default": env.db()}
else:
    DATABASES = {
        "default": {
            "ENGINE": env("DB_ENGINE", default="django.db.backends.sqlite3"),
            "NAME": env("DB_NAME", default=os.path.join(BASE_DIR, "TestDB.sqlite3")),
            "USER": env("DB_USER", default=""),
            "PASSWORD": env("DB_PASSWORD", default=""),
            "HOST": env("DB_HOST", default=""),
            "PORT": env("DB_PORT", default=""),
            "OPTIONS": {
                "timeout": 30,  # seconds to wait on a locked DB before raising OperationalError
            },
        }
    }

# SQLite: enable WAL so reads (list/search) don't block session writes from
# concurrent requests like notification polling.
from django.db.backends.signals import connection_created


def _configure_sqlite_connection(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")


connection_created.connect(_configure_sqlite_connection)

# ========================================
# CACHE (optional Redis when REDIS_URL is set)
# ========================================
# Fresh clones / runserver keep Django's default LocMem cache.
# Docker Compose sets REDIS_URL so the Redis service is actually used
# (requires django-redis in requirements.txt).
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
            },
            "KEY_PREFIX": "joydigi",
        }
    }

# ========================================
# STATIC & MEDIA FILES
# ========================================
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media/")

# ========================================
# AUTHENTICATION & SECURITY
# ========================================
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTH_USER_MODEL = "joydigi_auth.JoydigiUser"

X_FRAME_OPTIONS = "SAMEORIGIN"

# ========================================
# TEMPLATES
# ========================================
# In production (DEBUG=False) these are wrapped in the cached template
# loader so Django compiles each template once per process instead of
# re-parsing it (and re-running joydigi_dbtemplate's DB-lookup chain) on
# every include, on every request. Left uncached in DEBUG so template
# edits during development are picked up without restarting the server.
_TEMPLATE_LOADERS = [
    "joydigi_dbtemplate.loaders.Loader",
    ("django.template.loaders.filesystem.Loader", [BASE_DIR / THEME_APP / "templates"]),
    "django.template.loaders.app_directories.Loader",
    ("django.template.loaders.filesystem.Loader", [BASE_DIR / "templates"]),
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Joydigi dynamic context processors
                "joydigi.config.get_MENUS",
                "base.context_processors.get_companies",
                "base.context_processors.white_labelling_company",
                "base.context_processors.doc_base_url",
                "base.context_processors.timerunner_enabled",
                "base.context_processors.get_initial_prefix",
                "base.context_processors.enable_late_come_early_out_tracking",
                "base.context_processors.enable_profile_edit",
                "base.context_processors.export_access_enabled",
                "base.context_processors.navbar_languages",
                "joydigi_crumbs.context_processors.breadcrumbs",
                # Phase GLOBAL-ADMIN-PERF-B — TEMPORARY. Must stay last:
                # it records the instant the processors above it have
                # finished, which is the boundary between the view's own
                # work and template rendering. Returns {}.
                "joydigi.perf_timing.timing_context_mark",
            ],
            "loaders": (
                _TEMPLATE_LOADERS
                if DEBUG
                else [("django.template.loaders.cached.Loader", _TEMPLATE_LOADERS)]
            ),
        },
    },
]

WSGI_APPLICATION = "joydigi.wsgi.application"

# ========================================
# INTERNATIONALIZATION
# ========================================
LANGUAGE_CODE = "vi"
# JoyDigi operates in Vietnam: attendance, shifts and every
# server-stamped business timestamp are defined in Vietnam local time.
#
# Phase ATT-TIME-2I: this is deliberately a hardcoded business rule and
# no longer `env("TIME_ZONE", ...)`. Production was found running
# Asia/Kolkata (UTC+05:30) — the upstream product's default, left behind
# in the deployment environment — which silently wrote every attendance
# wall-clock 1h30m early. A value that shifts every recorded time in the
# system is not a per-deployment knob, and a stale environment entry
# must not be able to reintroduce that bug.
#
# Django exports this to the process clock itself on POSIX
# (`os.environ["TZ"]` + `time.tzset()`, django/conf/__init__.py), so no
# separate timezone mechanism is added here. No fixed offset is applied
# anywhere; conversion is done by the IANA timezone database.
TIME_ZONE = "Asia/Ho_Chi_Minh"
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ("vi", "Tiếng Việt"),
]

LOCALE_PATHS = [join(BASE_DIR, "joydigi", "locale")]

# ========================================
# LOGGING, MESSAGES, OTHER GLOBALS
# ========================================

# Phase NOTIFY-2. Until now this project configured no `LOGGING` at all,
# which is not the same as "logging goes to the default place": with no
# handler anywhere, Python falls back to its `lastResort` handler, and
# that emits WARNING and above only. Every `logger.info` in the codebase
# was therefore written to nowhere — including the one line that says
# push notifications are switched off because no Firebase credential is
# configured. A feature could be silently disabled for weeks and leave
# no trace to find it by, which is exactly what happened.
#
# Deliberately small: one console handler, because gunicorn's stdout is
# already captured by journald on this deployment and a second
# destination would only be a second thing to rotate. `django.request`
# and `django.db.backends` keep their own levels so turning this up does
# not also turn on SQL echo.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    # WARNING at the root, deliberately. The handler lives here so every
    # logger can reach it, but Python does not consult an ancestor
    # logger's level when a record propagates — only the handler's. So the
    # named loggers below still emit INFO while every third-party library
    # stays quiet, which is the difference between an observable
    # notification path and a journal full of urllib3.
    "root": {
        "handlers": ["console"],
        "level": env("LOG_LEVEL", default="WARNING"),
    },
    "loggers": {
        # The notification path, which is the reason this block exists.
        # INFO so the "why was nothing pushed" states are visible; these
        # are emitted once per scheduler run, never once per employee.
        "joydigi_api.push": {"level": "INFO", "propagate": True},
        "attendance.methods.reminders": {"level": "INFO", "propagate": True},
        "attendance.methods.end_of_day": {"level": "INFO", "propagate": True},
        # `attendance/scheduler.py` logs through `base.backends`'s logger
        # rather than its own module name, so that is the name to raise.
        "attendance.scheduler": {"level": "INFO", "propagate": True},
        "base.backends": {"level": "INFO", "propagate": True},
        # Left at WARNING on purpose. `django.db.backends` at DEBUG logs
        # every query, and `apscheduler` at INFO logs a line per job per
        # tick — with two jobs on a one-minute interval that is ~2900
        # lines a day saying only that nothing was due.
        "django.db.backends": {"level": "WARNING", "propagate": False},
        "apscheduler": {"level": "WARNING", "propagate": False},
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MESSAGE_TAGS = {
    messages.DEBUG: "oh-alert--warning",
    messages.INFO: "oh-alert--info",
    messages.SUCCESS: "oh-alert--success",
    messages.WARNING: "oh-alert--warning",
    messages.ERROR: "oh-alert--danger",
}

LOGIN_URL = "/login"
SIMPLE_HISTORY_REVERT_DISABLED = True

DJANGO_NOTIFICATIONS_CONFIG = {
    "USE_JSONFIELD": True,
    "SOFT_DELETE": True,
    "USE_WATCHED": True,
    "NOTIFICATIONS_STORAGE": "notifications.storage.DatabaseStorage",
    "TEMPLATE": "notifications.html",
}

# ========================================
# JOYDIGI-SPECIFIC SETTINGS
# ========================================
WHITE_LABELLING = False
NESTED_SUBORDINATE_VISIBILITY = False
TWO_FACTORS_AUTHENTICATION = False

SIDEBARS = [
    "base",
    "employee",
    "attendance",
    "leave",
]

# Audit logging is opt-in: the joydigi_audit app registers models explicitly
# through its registry, driven by AuditModelConfig and a default whitelist
# (Employee, EmployeeWorkInformation, EmployeeBankDetails).
AUDITLOG_INCLUDE_ALL_MODELS = False
AUDITLOG_EXCLUDE_TRACKING_MODELS = (
    # "<app_name>",
    # "<app_name>.<model>"
)

EMAIL_BACKEND = "base.backends.ConfiguredEmailBackend"
EMAIL_NOTIFICATIONS_ENABLED = env.bool("EMAIL_NOTIFICATIONS_ENABLED", default=False)
ENABLE_DB_BACKUP = env.bool("ENABLE_DB_BACKUP", default=True)

"""
DB_INIT_PASSWORD: str

The password used for database setup and initialization. This password is a
48-character alphanumeric string generated using a UUID to ensure high entropy and security.
"""
DB_INIT_PASSWORD = env(
    "DB_INIT_PASSWORD", default="d3f6a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d"
)

# ========================================
# PERMISSIONS / CUSTOM LOGIC
# ========================================
# When True, group permissions are scoped per company via
# base.models.CompanyGroupAssignment (resolved by CompanyScopedBackend).
# When False, legacy behavior: user.groups grant permissions globally.
# Instant rollback switch: set the COMPANY_SCOPED_PERMISSIONS env var to False.
COMPANY_SCOPED_PERMISSIONS = env.bool("COMPANY_SCOPED_PERMISSIONS", default=True)

NO_PERMISSION_MODALS = [
    "companygroupassignment",
    "historicalbonuspoint",
    "assetreport",
    "assetdocuments",
    "returnimages",
    "holiday",
    "companyleave",
    "historicalavailableleave",
    "historicalleaverequest",
    "historicalleaveallocationrequest",
    "leaverequestconditionapproval",
    "historicalcompensatoryleaverequest",
    "employeepastleaverestrict",
    "overrideleaverequests",
    "historicalrotatingworktypeassign",
    "employeeshiftday",
    "historicalrotatingshiftassign",
    "historicalworktyperequest",
    "historicalshiftrequest",
    "multipleapprovalmanagers",
    "attachment",
    "announcementview",
    "emaillog",
    "driverviewed",
    "dashboardemployeecharts",
    "attendanceallowedip",
    "tracklatecomeearlyout",
    "historicalcontract",
    "overrideattendance",
    "overrideleaverequest",
    "overrideworkinfo",
    "multiplecondition",
    "historicalpayslip",
    "reimbursementmultipleattachment",
    "workrecord",
    "historicalticket",
    "skill",
    "historicalcandidate",
    "rejectreason",
    "historicalrejectedcandidate",
    "rejectedcandidate",
    "stagefiles",
    "stagenote",
    "questionordering",
    "recruitmentsurveyordering",
    "recruitmentsurveyanswer",
    "recruitmentgeneralsetting",
    "resume",
    "recruitmentmailtemplate",
    "profileeditfeature",
]

FILE_STORAGE = FileSystemStorage(location="csv_tmp/")

JOYDIGI_DATE_FORMATS = {
    "DD/MM/YY": "%d/%m/%y",
    "DD-MM-YYYY": "%d-%m-%Y",
    "DD.MM.YYYY": "%d.%m.%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY/MM/DD": "%Y/%m/%d",
    "MMMM D, YYYY": "%B %d, %Y",
    "DD MMMM, YYYY": "%d %B, %Y",
    "MMM. D, YYYY": "%b. %d, %Y",
    "D MMM. YYYY": "%d %b. %Y",
    "dddd, MMMM D, YYYY": "%A, %B %d, %Y",
}

JOYDIGI_TIME_FORMATS = {
    "hh:mm A": "%I:%M %p",  # 12-hour format
    "HH:mm": "%H:%M",  # 24-hour format
    "HH:mm:ss.SSSSSS": "%H:%M:%S.%f",  # 24-hour format with seconds and microseconds
}

BIO_DEVICE_THREADS = {}

DYNAMIC_URL_PATTERNS = []

APP_URLS = [
    "base.urls",
    "employee.urls",
]

APPS = [
    "auth",
    "base",
    "employee",
    "joydigi_documents",
]

# CompanyScopedBackend subclasses ModelBackend; it behaves identically while
# COMPANY_SCOPED_PERMISSIONS is False. It must REPLACE ModelBackend (Django
# unions grants across backends, so listing both would keep global perms).
AUTHENTICATION_BACKENDS = [
    "base.auth_backends.CompanyScopedBackend",
]

# ========================================
# PRODUCTION SECURITY GATES
# ========================================
# Fail closed when DEBUG=False or JOYDIGI_ENV=production. Local DEBUG=True
# tutorials keep insecure-but-documented defaults for open-source onboarding.
from joydigi.settings.security import (  # noqa: E402
    apply_secure_defaults,
    is_production_mode,
    validate_production_secrets,
)

IS_PRODUCTION = is_production_mode(DEBUG, JOYDIGI_ENV)

if IS_PRODUCTION:
    validate_production_secrets(SECRET_KEY, ALLOWED_HOSTS, DB_INIT_PASSWORD)

if not DEBUG:
    globals().update(apply_secure_defaults(env, DEBUG))
