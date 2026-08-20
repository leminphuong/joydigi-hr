"""
base/sidebar.py

Settings menu registrations for the base app.

Sections registered:
  - General       : general settings, permissions, tags
  - Base          : department, job position, job role, company
  - Appearance : color theme (only when joydigi_theme is installed)
  - Integrations  : linkedin, ldap, google meet, whatsapp
"""

from django.apps import apps
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from joydigi.menu import settings_menu

MENU = _("Bảng tin")
IMG_SRC = "images/ui/announcement.svg"
SUBMENUS = [
    {
        "menu": _("Bản tin nội bộ"),
        "redirect": reverse_lazy("bulletin"),
    },
]

# ---------------------------------------------------------------------------
# Accessibility functions
# ---------------------------------------------------------------------------


def system_preferences_accessibility(request, submenu, user_perms, *args, **kwargs):
    return any(
        request.user.has_perm(p)
        for p in [
            "base.change_announcementexpire",
            "base.view_dynamicpagination",
            "joydigi_audit.view_accountblockunblock",
            "employee.change_employeegeneralsetting",
            "joydigi_audit.view_historytrackingfields",
            "payroll.view_payrollsettings",
            "base.view_company",
            "base.view_companylanguagesetting",
        ]
    )


def general_settings_accessibility(request, submenu, user_perms, *args, **kwargs):
    return system_preferences_accessibility(
        request, submenu, user_perms, *args, **kwargs
    )


def employee_permission_accessibility(request, submenu, user_perms, *args, **kwargs):
    # Direct permission assign: superadmin only
    return request.user.is_superuser


def accessibility_restriction_accessibility(
    request, submenu, user_perms, *args, **kwargs
):
    return request.user.has_perm("auth.view_permission")


def user_group_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.is_superuser


def date_settings_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("base.view_company")


def history_tags_accessibility(request, submenu, user_perms, *args, **kwargs):
    return any(
        request.user.has_perm(p)
        for p in [
            "base.view_tags",
            "employee.view_employeetag",
            "joydigi_audit.view_audittag",
        ]
    )


def audit_tracking_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("joydigi_audit.view_auditmodelconfig")


def audit_history_accessibility(request, submenu, user_perms, *args, **kwargs):
    return any(
        request.user.has_perm(p)
        for p in [
            "joydigi_audit.view_audittag",
            "joydigi_audit.view_auditmodelconfig",
        ]
    )


def department_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("base.view_department")


def job_position_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("base.view_jobposition")


def job_role_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("base.view_jobrole")


def company_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("base.view_company")


def holidays_settings_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("base.view_holidays")


def company_leaves_settings_accessibility(
    request, submenu, user_perms, *args, **kwargs
):
    return request.user.has_perm("base.view_companyleaves")


def color_theme_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("joydigi_theme.view_joydigicolortheme")


def linkedin_accessibility(request, submenu, user_perms, *args, **kwargs):
    return apps.is_installed("recruitment") and request.user.has_perm(
        "recruitment.view_linkedinaccount"
    )


def ldap_accessibility(request, submenu, user_perms, *args, **kwargs):
    return apps.is_installed("joydigi_ldap") and any(
        request.user.has_perm(p)
        for p in ["joydigi_ldap.add_ldapsettings", "joydigi_ldap.update_ldapsettings"]
    )


def google_meet_accessibility(request, submenu, user_perms, *args, **kwargs):
    return apps.is_installed("joydigi_meet") and request.user.has_perm(
        "joydigi_meet.view_googlecloudcredential"
    )


def whatsapp_accessibility(request, submenu, user_perms, *args, **kwargs):
    return apps.is_installed("whatsapp") and request.user.has_perm(
        "whatsapp.add_whatsappcredientials"
    )


def default_export_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("base.view_defaultexportpermission")


# ---------------------------------------------------------------------------
# 1. General settings section
# ---------------------------------------------------------------------------


@settings_menu.register
class GeneralSettings:
    title = _("Cài đặt chung")
    order = 1
    items = [
        {
            "label": _("Thiết lập chung"),
            "url": reverse_lazy("system-preferences-view"),
            "accessibility": system_preferences_accessibility,
            "search_entries": [
                {
                    "text": _("Số ngày hiển thị bản tin"),
                    "description": _("Tự ẩn bản tin sau số ngày đã chọn"),
                    "anchor": "setting-default-expire-days",
                },
                {
                    "text": _("Số dòng mỗi trang"),
                    "description": _("Số dòng được hiển thị trên một trang"),
                    "anchor": "setting-default-records-per-page",
                },
                {
                    "text": _("Tiền tố mã nhân viên"),
                    "description": _("Phần chữ đứng trước mã nhân viên, ví dụ NV0001"),
                    "anchor": "setting-badge-id-prefix",
                },
                {
                    "text": _("Ký hiệu tiền tệ"),
                    "description": _("Ký hiệu dùng khi hiển thị số tiền"),
                    "anchor": "setting-currency-symbol",
                },
                {
                    "text": _("Vị trí ký hiệu tiền tệ"),
                    "description": _("Đặt ký hiệu trước hoặc sau số tiền"),
                    "anchor": "setting-currency-position",
                },
                {
                    "text": _("Cách hiển thị ngày"),
                    "description": _("Cách ngày tháng được hiển thị trong hệ thống"),
                    "anchor": "setting-date-format",
                },
                {
                    "text": _("Cách hiển thị giờ"),
                    "description": _("Hiển thị giờ theo kiểu 12 giờ hoặc 24 giờ"),
                    "anchor": "setting-time-format",
                },
                {
                    "text": _("Giới hạn đăng nhập"),
                    "description": _("Cho phép quản trị viên khóa hoặc mở đăng nhập của nhân viên"),
                    "anchor": "setting-restrict-login-account",
                },
                {
                    "text": _("Giới hạn sửa hồ sơ"),
                    "description": _("Không cho nhân viên tự sửa hồ sơ của mình"),
                    "anchor": "setting-restrict-profile-edit",
                },
                {
                    "text": _("Theo dõi thông tin công việc"),
                    "description": _("Lưu lại các thay đổi về thông tin công việc của nhân viên"),
                    "anchor": "setting-work-info-tracking",
                },
                {
                    "text": _("Thông tin cần theo dõi"),
                    "description": _("Chọn thông tin sẽ được lưu trong lịch sử thay đổi"),
                    "anchor": "setting-tracking-fields",
                },
                {
                    "text": _("Ngôn ngữ sử dụng"),
                    "description": _("Chọn ngôn ngữ được phép hiển thị cho công ty"),
                    "anchor": "companyLanguageSettings",
                },
            ],
        },
        {
            "label": _("Quyền xem menu"),
            "url": reverse_lazy("user-accessibility"),
            "accessibility": accessibility_restriction_accessibility,
            "search_entries": [
                {
                    "text": _("Quyền xem menu"),
                    "description": _("Chọn vai trò được xem từng mục trong menu"),
                },
            ],
        },
        {
            "label": _("Vai trò và quyền hạn"),
            "url": reverse_lazy("user-group-view"),
            "accessibility": user_group_accessibility,
            "search_entries": [
                {
                    "text": _("Vai trò và quyền hạn"),
                    "description": _("Phân quyền cho quản trị viên, trưởng nhóm và nhân viên"),
                },
                {
                    "text": _("Vai trò"),
                    "description": _("Quản lý ba vai trò có sẵn trong hệ thống"),
                },
                {
                    "text": _("Quyền của nhân viên"),
                    "description": _("Cấp hoặc thu hồi quyền riêng cho từng nhân viên"),
                },
            ],
        },
        {
            "label": _("Nhật ký thay đổi"),
            "url": reverse_lazy("audit-history-view"),
            "accessibility": audit_history_accessibility,
            "search_entries": [
                {
                    "text": _("Nhật ký thay đổi"),
                    "description": _("Xem lịch sử thay đổi dữ liệu trong hệ thống"),
                },
                {
                    "text": _("Nhãn nhật ký"),
                    "description": _("Dùng nhãn để phân loại các thay đổi"),
                },
                {
                    "text": _("Theo dõi thay đổi"),
                    "description": _("Chọn loại dữ liệu cần lưu lịch sử thay đổi"),
                },
                {
                    "text": _("Dữ liệu được theo dõi"),
                    "description": _("Các loại dữ liệu đang được lưu lịch sử thay đổi"),
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# 2. Organization section
# ---------------------------------------------------------------------------


@settings_menu.register
class BaseSettings:
    title = _("Tổ chức")
    order = 2
    items = [
        {
            "label": _("Công ty"),
            "url": reverse_lazy("company-view"),
            "accessibility": company_accessibility,
            "search_entries": [
                {
                    "text": _("Tên công ty"),
                    "description": _("Tên đầy đủ của công ty"),
                },
                {"text": _("Địa chỉ công ty"), "description": _("Địa chỉ làm việc")},
                {
                    "text": _("Quốc gia"),
                    "description": _("Quốc gia nơi công ty đăng ký"),
                },
                {"text": _("Tỉnh hoặc thành phố"), "description": _("Tỉnh hoặc thành phố")},
                {"text": _("Quận hoặc huyện"), "description": _("Quận, huyện hoặc khu vực")},
                {"text": _("Mã bưu chính"), "description": _("Mã bưu chính của địa chỉ")},
                {
                    "text": _("Biểu trưng công ty"),
                    "description": _("Hình đại diện của công ty"),
                },
            ],
        },
        {
            "label": _("Phòng ban"),
            "url": reverse_lazy("department-view"),
            "accessibility": department_accessibility,
            "search_entries": [
                {"text": _("Phòng ban"), "description": _("Tên phòng ban")},
                {
                    "text": _("Người quản lý phòng ban"),
                    "description": _("Người phụ trách phòng ban"),
                },
            ],
        },
        {
            "label": _("Chức danh"),
            "url": reverse_lazy("job-position-view"),
            "accessibility": job_position_accessibility,
            "search_entries": [
                {
                    "text": _("Chức danh"),
                    "description": _("Tên chức danh công việc"),
                },
            ],
        },
        {
            "label": _("Vai trò công việc"),
            "url": reverse_lazy("job-role-view"),
            "accessibility": job_role_accessibility,
            "search_entries": [
                {"text": _("Vai trò công việc"), "description": _("Tên vai trò công việc")},
            ],
        },
        {
            "label": _("Ngày nghỉ hằng tuần"),
            "url": reverse_lazy("company-leaves-view"),
            "accessibility": company_leaves_settings_accessibility,
            "search_entries": [
                {
                    "text": _("Ngày nghỉ hằng tuần"),
                    "description": _("Thiết lập ngày nghỉ lặp lại hằng tuần"),
                },
                {
                    "text": _("Tuần áp dụng"),
                    "description": _("Chọn tuần trong tháng được áp dụng ngày nghỉ"),
                },
                {
                    "text": _("Thứ được nghỉ"),
                    "description": _("Chọn ngày trong tuần được nghỉ"),
                },
            ],
        },
        # {
        #     "label": _("Public Holidays"),
        #     "url": reverse_lazy("holidays-view"),
        #     "accessibility": holidays_settings_accessibility,
        #     "search_entries": [
        #         {"text": _("Public Holiday"), "description": _("Name of the public holiday")},
        #         {"text": _("Holiday Start Date"), "description": _("Start date")},
        #         {"text": _("Holiday End Date"), "description": _("End date")},
        #         {"text": _("Recurring Holiday"), "description": _("Whether this holiday repeats every year")},
        #     ],
        # },
    ]


# ---------------------------------------------------------------------------
# 5. Appearance section (only when joydigi_theme is installed)
# ---------------------------------------------------------------------------


@settings_menu.register
class ThemeManagerSettings:
    title = _("Giao diện")
    order = 10
    condition = lambda self, request: apps.is_installed("joydigi_theme")
    items = [
        {
            "label": _("Màu giao diện"),
            "url": reverse_lazy("joydigi_theme:color_theme_view"),
            "accessibility": color_theme_accessibility,
            "search_entries": [
                {
                    "text": _("Màu giao diện"),
                    "description": _("Chọn màu hiển thị cho hệ thống"),
                },
                {
                    "text": _("Quản lý giao diện"),
                    "description": _("Thay đổi giao diện bằng các bộ màu có sẵn"),
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# 6. Integrations section
# ---------------------------------------------------------------------------


@settings_menu.register
class IntegrationsSettings:
    title = _("Kết nối và sao lưu")
    order = 11
    items = [
        {
            "label": _("LinkedIn"),
            "url": reverse_lazy("linkedin-integration-setting"),
            "accessibility": linkedin_accessibility,
            "search_entries": [
                {
                    "text": _("LinkedIn Integration"),
                    "description": _("Connect LinkedIn for job posting"),
                },
                {
                    "text": _("LinkedIn API Token"),
                    "description": _("API token for LinkedIn integration"),
                },
                {
                    "text": _("Enable LinkedIn"),
                    "description": _("Activate LinkedIn integration for job posting"),
                },
                {
                    "text": _("Post on LinkedIn"),
                    "description": _("Automatically post job openings to LinkedIn"),
                },
            ],
        },
        {
            "label": _("LDAP"),
            "url": reverse_lazy("ldap-settings"),
            "accessibility": ldap_accessibility,
            "search_entries": [
                {
                    "text": _("LDAP"),
                    "description": _("Connect to LDAP for employee authentication"),
                },
                {
                    "text": _("LDAP Server"),
                    "description": _("LDAP server address e.g. ldap://127.0.0.1:389"),
                },
                {
                    "text": _("Bind DN"),
                    "description": _("LDAP bind distinguished name"),
                },
                {
                    "text": _("Base DN"),
                    "description": _("LDAP base distinguished name for user search"),
                },
            ],
        },
        {
            "label": _("Google Meet"),
            "url": reverse_lazy("gmeet-setting"),
            "accessibility": google_meet_accessibility,
            "search_entries": [
                {
                    "text": _("Google Meet"),
                    "description": _("Configure Google Meet for interviews"),
                },
                {
                    "text": _("Google Cloud Project ID"),
                    "description": _("Google Cloud project ID"),
                },
                {
                    "text": _("Google Client ID"),
                    "description": _("OAuth client ID for Google Meet"),
                },
                {
                    "text": _("Google Client Secret"),
                    "description": _("OAuth client secret for Google Meet"),
                },
                {
                    "text": _("Enable Google Meet"),
                    "description": _(
                        "Activate Google Meet integration for video interviews"
                    ),
                },
                {
                    "text": _("Redirect URIs"),
                    "description": _("Authorised OAuth redirect URIs for Google Meet"),
                },
            ],
        },
        {
            "label": _("WhatsApp"),
            "url": reverse_lazy("whatsapp-credential-view"),
            "accessibility": whatsapp_accessibility,
            "search_entries": [
                {
                    "text": _("WhatsApp"),
                    "description": _(
                        "Configure WhatsApp Business API for notifications"
                    ),
                },
                {
                    "text": _("Meta Token"),
                    "description": _("WhatsApp Business API access token"),
                },
                {
                    "text": _("Meta Business ID"),
                    "description": _("WhatsApp Meta business account ID"),
                },
                {
                    "text": _("Meta Phone Number"),
                    "description": _("WhatsApp business phone number"),
                },
                {
                    "text": _("Webhook Token"),
                    "description": _("Token for verifying WhatsApp webhook callbacks"),
                },
                {
                    "text": _("Enable WhatsApp"),
                    "description": _("Activate WhatsApp Business API integration"),
                },
                {
                    "text": _("Meta Phone Number ID"),
                    "description": _("WhatsApp Meta phone number identifier"),
                },
            ],
        },
    ]
