import json
import re
from datetime import datetime

from django import template
from django.core.paginator import Page, Paginator
from django.template.defaultfilters import register
from django.utils import timezone

from base.methods import get_pagination
from base.models import MultipleApprovalManagers
from base.roles import is_checkin_admin as user_is_checkin_admin
from base.roles import is_checkin_leader as user_is_checkin_leader
from employee.models import Employee, EmployeeWorkInformation
from joydigi.menu.settings_menu import get_settings_menu

register = template.Library()


_NOTIFICATION_TEXTS_VI = {
    "Your roster has been published.": "Lịch làm việc của bạn đã được công bố.",
    "You have a new leave request to validate.": "Có đơn xin phép mới đang chờ bạn duyệt.",
    "New leave type is assigned to you": "Bạn đã được cấp một loại nghỉ phép mới.",
    "Your leave allocation request has been approved": "Yêu cầu cấp ngày phép của bạn đã được duyệt.",
    "Your leave allocation request has been rejected": "Yêu cầu cấp ngày phép của bạn đã bị từ chối.",
    "Your leave request has received a comment.": "Đơn xin phép của bạn có bình luận mới.",
    "Your leave allocation request has received a comment.": "Yêu cầu cấp ngày phép của bạn có bình luận mới.",
    "Your compensatory leave request has been approved": "Đơn nghỉ bù của bạn đã được duyệt.",
    "Your compensatory leave request has been rejected": "Đơn nghỉ bù của bạn đã bị từ chối.",
    "Your compensatory leave request has received a comment.": "Đơn nghỉ bù của bạn có bình luận mới.",
    "Your attendance request has received a comment.": "Yêu cầu chấm công của bạn có bình luận mới.",
    "Your work type request has been rejected.": "Yêu cầu hình thức làm việc của bạn đã bị từ chối.",
    "Your work type request has been canceled.": "Yêu cầu hình thức làm việc của bạn đã được hủy.",
    "Your work type request has been approved.": "Yêu cầu hình thức làm việc của bạn đã được duyệt.",
    "Your work type request has been deleted.": "Yêu cầu hình thức làm việc của bạn đã được xóa.",
    "Your shift request has been canceled.": "Yêu cầu đổi ca của bạn đã được hủy.",
    "Your shift request has been rejected.": "Yêu cầu đổi ca của bạn đã bị từ chối.",
    "Your shift request has been approved.": "Yêu cầu đổi ca của bạn đã được duyệt.",
    "Your shift request has been deleted.": "Yêu cầu đổi ca của bạn đã được xóa.",
    "Your shift request has received a comment.": "Yêu cầu đổi ca của bạn có bình luận mới.",
    "Your work type request has received a comment.": "Yêu cầu hình thức làm việc của bạn có bình luận mới.",
    "You are added to rotating work type": "Bạn đã được thêm vào lịch luân phiên hình thức làm việc.",
    "You are added to rotating shift": "Bạn đã được thêm vào lịch luân phiên ca làm việc.",
    "Your Work Type has been changed.": "Hình thức làm việc của bạn đã được thay đổi.",
    "Your shift has been changed.": "Ca làm việc của bạn đã được thay đổi.",
    "Shift Changes notification": "Thông báo thay đổi ca làm việc.",
    "Shift changes notification, Requested date expired.": "Yêu cầu thay đổi ca đã hết hạn.",
    "Work Type Changes notification": "Thông báo thay đổi hình thức làm việc.",
    "Work type changes notification, Requested date expired.": "Yêu cầu thay đổi hình thức làm việc đã hết hạn.",
}


_NOTIFICATION_PATTERNS_VI = (
    (r"^Your roster from (.+) has been published\.$", r"Lịch làm việc của bạn từ \1 đã được công bố."),
    (r"^New leave request created for (.+)\.$", r"Đã tạo đơn xin phép mới cho \1."),
    (r"^Leave request updated for (.+)\.$", r"Đơn xin phép của \1 đã được cập nhật."),
    (r"^Your (.+) leave type updated\.$", r"Loại nghỉ phép \1 của bạn đã được cập nhật."),
    (r"^New leave allocation request created for (.+)\.$", r"Đã tạo yêu cầu cấp ngày phép mới cho \1."),
    (r"^Leave allocation request updated for (.+)\.$", r"Yêu cầu cấp ngày phép của \1 đã được cập nhật."),
    (r"^(.+)'s leave request has received a comment\.$", r"Đơn xin phép của \1 có bình luận mới."),
    (r"^(.+)'s leave allocation request has received a comment\.$", r"Yêu cầu cấp ngày phép của \1 có bình luận mới."),
    (r"^(.+)'s [Cc]ompensatory leave request has received a comment\.$", r"Đơn nghỉ bù của \1 có bình luận mới."),
    (r"^Your attendance for the date (.+) is validated\.?$", r"Chấm công ngày \1 của bạn đã được xác nhận."),
    (r"^Your (.+)'s attendance overtime approved\.$", r"Làm thêm giờ ngày \1 của bạn đã được duyệt."),
    (r"^Overtime approved for (.+)'s attendance$", r"Đã duyệt làm thêm giờ ngày \1."),
    (r"^Your attendance request for (.+) is rejected\.?$", r"Yêu cầu chấm công ngày \1 của bạn đã bị từ chối."),
    (r"^(.+) requested revalidation for\s+(.+) attendance$", r"\1 yêu cầu xác nhận lại chấm công ngày \2."),
    (r"^(.+)'s attendance request has received a comment\.$", r"Yêu cầu chấm công của \1 có bình luận mới."),
    (r"^Comment under the announcement (.+)\.$", r"Bản tin “\1” có bình luận mới."),
    (r"^You have new work type request to approve(?: for)?\s*(.*)$", r"Có yêu cầu hình thức làm việc mới đang chờ bạn duyệt: \1"),
    (r"^You have new shift request to approve(?: for)?\s*(.*)$", r"Có yêu cầu đổi ca mới đang chờ bạn duyệt: \1"),
    (r"^You have a new shift reallocation request to approve for (.+)\.$", r"Có yêu cầu đổi ca của \1 đang chờ bạn duyệt."),
    (r"^You have a new shift reallocation request from (.+)\.$", r"Bạn có yêu cầu đổi ca mới từ \1."),
    (r"^(.+) is available for shift reallocation\.$", r"\1 đang sẵn sàng đổi ca."),
    (r"^(.+)'s shift request has received a comment\.$", r"Yêu cầu đổi ca của \1 có bình luận mới."),
    (r"^(.+)'s work type request has received a comment\.$", r"Yêu cầu hình thức làm việc của \1 có bình luận mới."),
)


@register.filter(name="notification_text_vi")
def notification_text_vi(value):
    """Việt hóa cả thông báo mới lẫn nội dung tiếng Anh đã lưu trước đây."""
    text = str(value or "").strip()
    if not text:
        return ""
    translated = _NOTIFICATION_TEXTS_VI.get(text)
    if translated:
        return translated
    for pattern, replacement in _NOTIFICATION_PATTERNS_VI:
        if re.match(pattern, text, flags=re.IGNORECASE):
            return re.sub(pattern, replacement, text, flags=re.IGNORECASE).strip()
    return text


@register.filter(name="relative_time_vi")
def relative_time_vi(value):
    """Hiển thị thời gian tương đối bằng tiếng Việt, không phụ thuộc cookie ngôn ngữ."""
    if not isinstance(value, datetime):
        return ""
    current = timezone.now()
    if timezone.is_naive(value) and timezone.is_aware(current):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    elif timezone.is_aware(value) and timezone.is_naive(current):
        value = timezone.make_naive(value, timezone.get_current_timezone())

    seconds = max(0, int((current - value).total_seconds()))
    if seconds < 10:
        return "vừa xong"
    if seconds < 60:
        return f"{seconds} giây trước"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} phút trước"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} giờ trước"
    days = hours // 24
    if days < 30:
        return f"{days} ngày trước"
    months = days // 30
    if months < 12:
        return f"{months} tháng trước"
    return f"{days // 365} năm trước"


@register.filter(name="is_checkin_admin")
def is_checkin_admin(user):
    return user_is_checkin_admin(user)


@register.filter(name="is_checkin_leader")
def is_checkin_leader(user):
    return user_is_checkin_leader(user)


@register.simple_tag
def pending_checkin_approvals(user):
    """Số đơn đang chờ trong phạm vi mà quản trị viên/trưởng nhóm được duyệt."""
    if not user_is_checkin_leader(user):
        return 0
    try:
        from attendance.models import Attendance
        from leave.models import LeaveRequest

        employee = _get_employee_of_user(user)
        if user_is_checkin_admin(user):
            employee_ids = Employee.objects.filter(is_active=True).values_list(
                "pk", flat=True
            )
        elif employee:
            employee_ids = Employee.objects.filter(
                is_active=True,
                employee_work_info__reporting_manager_id=employee,
            ).values_list("pk", flat=True)
        else:
            return 0
        return LeaveRequest.objects.filter(
            employee_id_id__in=employee_ids, status="requested"
        ).count() + Attendance.objects.filter(
            employee_id_id__in=employee_ids,
            is_validate_request=True,
            is_validate_request_approved=False,
        ).count()
    except (DatabaseError, AttributeError):
        return 0


@register.filter
def equals(value, arg):
    """Check if value equals arg"""
    return value == arg


def _get_employee_of_user(user):
    """
    Resolve the Employee for a user once per request and cache it on the
    user object - this tag/filter is called once per row in list views, and
    without this cache each call re-issues the same lookup query.
    """
    if not hasattr(user, "_joydigi_employee_cache"):
        user._joydigi_employee_cache = Employee.objects.filter(
            employee_user_id=user
        ).first()
    return user._joydigi_employee_cache


@register.simple_tag
def is_manager_of(user, instance, field_name="employee_id"):
    employee = _get_employee_of_user(user)

    target_employee = getattr(instance, field_name, None)

    if not hasattr(user, "_joydigi_is_manager_of_cache"):
        user._joydigi_is_manager_of_cache = {}
    cache = user._joydigi_is_manager_of_cache
    key = (getattr(employee, "id", None), getattr(target_employee, "id", None))
    if key not in cache:
        cache[key] = EmployeeWorkInformation.objects.filter(
            reporting_manager_id=employee, employee_id=target_employee
        ).exists()
    return cache[key]


@register.filter(name="is_reportingmanager")
def is_reportingmanager(user):
    """

    This method will return true if the user employee profile is reporting manager to any employee
    """
    employee = _get_employee_of_user(user)
    return EmployeeWorkInformation.objects.filter(
        reporting_manager_id=employee
    ).exists()


@register.filter(name="is_leave_approval_manager")
def is_leave_approval_manager(user):
    """
    This method will return true if the user is comes in MultipleApprovalCondition model as approving manager
    """
    if hasattr(user, "_joydigi_is_leave_approval_manager_cache"):
        return user._joydigi_is_leave_approval_manager_cache
    employee = _get_employee_of_user(user)
    manager = (
        MultipleApprovalManagers.objects.entire()
        .filter(employee_id=employee.id)
        .exists()
        if employee
        else False
    )
    user._joydigi_is_leave_approval_manager_cache = manager
    return manager


@register.filter(name="check_manager")
def check_manager(user, instance):
    try:
        if isinstance(instance, Employee):
            return instance.employee_work_info.reporting_manager_id == user.employee_get
        return (
            user.employee_get
            == instance.employee_id.employee_work_info.reporting_manager_id
        )
    except:
        return False


@register.filter(name="filtersubordinates")
def filtersubordinates(user):
    """
    This method returns true if the user employee has corresponding related reporting manager object in EmployeeWorkInformation model
    args:
        user    : request.user
    """

    employee = user.employee_get
    employee_manages = employee.reporting_manager.all()
    return employee_manages.exists()


@register.filter(name="filter_field")
def filter_field(value):
    if value.endswith("_id"):
        value = value[:-3]
    if value.endswith("_ids"):
        value = value[:-4]
    splitted = value.split("__")

    return splitted[-1].replace("_", " ").capitalize()


@register.filter(name="user_perms")
def user_perms(perms):
    """
    permission names return method
    """
    return json.dumps(list(perms.values_list("codename", flat="True")))


@register.filter(name="all_user_perms")
def all_user_perms(user):
    """
    Return JSON list of effective permission codenames for a user for the
    currently selected company (group assignments + direct user permissions).
    """
    if not user:
        return json.dumps([])
    from base.auth_backends import get_effective_permission_codenames

    return json.dumps(get_effective_permission_codenames(user))


@register.filter(name="company_user_groups")
def company_user_groups(user):
    """
    Groups assigned to the user in the currently selected company.
    """
    from base.auth_backends import get_user_groups_for_company

    if not user:
        return []
    return list(get_user_groups_for_company(user))


@register.filter(name="abs_value")
def abs_value(value):
    """
    permission names return method
    """
    return abs(value)


@register.filter(name="startswith")
def startswith(value, arg):
    """Checks if the value starts with the provided argument."""
    return value.startswith(arg)


@register.filter(name="has_content")
def has_content(value):
    """Returns True if the input string has non-whitespace content."""
    if isinstance(value, str):
        return bool(value.strip())
    return True


@register.filter(name="readable")
def readable(value):
    try:
        value = value.replace("_", " ").replace("id", "").title()
    except:
        value = value
    return value


@register.simple_tag(takes_context=True)
def general_section_main(context):
    user = context["request"].user

    if not user.is_authenticated:
        return False

    return any(
        [
            user.has_perm("base.change_announcementexpire"),
            user.has_perm("base.view_dynamicpagination"),
            user.has_perm("joydigi_audit.view_accountblockunblock"),
            user.has_perm("offboarding.change_offboardinggeneralsetting"),
            user.has_perm("attendance.change_attendancegeneralsetting"),
            user.has_perm("payroll.change_payrollgeneralsetting"),
            user.has_perm("employee.change_employeegeneralsetting"),
            user.has_perm("payroll.change_encashmentgeneralsettings"),
            user.has_perm("joydigi_audit.view_historytrackingfields"),
            user.has_perm("payroll.view_payrollsettings"),
            user.has_perm("auth.view_permission"),
            user.has_perm("auth.view_group"),
            user.has_perm("base.view_company"),
            user.has_perm("base.view_tags"),
            user.has_perm("employee.view_employeetag"),
            user.has_perm("joydigi_audit.view_audittag"),
            user.has_perm("base.view_dynamicemailconfiguration"),
            user.has_perm("joydigi_backup.view_googledrivebackup"),
        ]
    )


@register.simple_tag(takes_context=True)
def general_section(context):
    user = context["request"].user

    if not user.is_authenticated:
        return False

    return any(
        [
            user.has_perm("base.change_announcementexpire"),
            user.has_perm("base.view_dynamicpagination"),
            user.has_perm("joydigi_audit.view_accountblockunblock"),
            user.has_perm("offboarding.change_offboardinggeneralsetting"),
            user.has_perm("attendance.change_attendancegeneralsetting"),
            user.has_perm("payroll.change_payrollgeneralsetting"),
            user.has_perm("employee.change_employeegeneralsetting"),
            user.has_perm("payroll.change_encashmentgeneralsettings"),
            user.has_perm("joydigi_audit.view_historytrackingfields"),
            user.has_perm("payroll.view_payrollsettings"),
        ]
    )


@register.simple_tag(takes_context=True)
def employee_section(context):
    user = context["request"].user

    if not user.is_authenticated:
        return False

    return any(
        [
            user.has_perm("base.view_worktype"),
            user.has_perm("base.view_rotatingworktype"),
            user.has_perm("base.view_employeeshift"),
            user.has_perm("base.view_rotatingshift"),
            user.has_perm("base.view_employeeshiftschedule"),
            user.has_perm("base.view_employeetype"),
            user.has_perm("employee.view_actiontype"),
            user.has_perm("employee.view_employeetag"),
        ]
    )


@register.simple_tag(takes_context=True)
def attendance_section(context):
    user = context["request"].user

    if not user.is_authenticated:
        return False

    return any(
        [
            user.has_perm("attendance.view_attendancevalidationcondition"),
            user.has_perm("base.view_biometricattendance"),
            user.has_perm("attendance.add_attendance"),
            user.has_perm("facedetection.add_facedetection"),
        ]
    )


@register.simple_tag(takes_context=True)
def show_section(context):
    user = context["request"].user

    if not user.is_authenticated:
        return False

    return any(
        [
            user.has_perm("attendance.view_attendancevalidationcondition"),
            user.has_perm("helpdesk.view_departmentmanager"),
            user.has_perm("helpdesk.view_tickettype"),
            user.has_perm("employee.view_employeetag"),
            user.has_perm("pms.add_bonuspointsetting"),
            user.has_perm("payroll.view_payslipautogenerate"),
            user.has_perm("leave.add_restrictleave"),
            user.has_perm("base.view_biometricattendance"),
            user.has_perm("attendance.add_attendance"),
            user.has_perm("facedetection.add_facedetection"),
            user.has_perm("recruitment.view_recruitment"),
            user.has_perm("recruitment.view_rejectreason"),
            user.has_perm("recruitment.add_recruitment"),
            user.has_perm("recruitment.add_linkedinaccount"),
            user.has_perm("joydigi_audit.view_accountblockunblock"),
            user.has_perm("offboarding.change_offboardinggeneralsetting"),
            user.has_perm("attendance.change_attendancegeneralsetting"),
            user.has_perm("payroll.change_payrollgeneralsetting"),
            user.has_perm("employee.change_employeegeneralsetting"),
            user.has_perm("payroll.change_encashmentgeneralsettings"),
            user.has_perm("payroll.view_payrollsettings"),
            user.has_perm("auth.view_permission"),
            user.has_perm("auth.view_group"),
            user.has_perm("joydigi_audit.view_audittag"),
            user.has_perm("joydigi_backup.view_googledrivebackup"),
            user.has_perm("joydigi_ldap.add_ldapsettings"),
            user.has_perm("joydigi_ldap.update_ldapsettings"),
            user.has_perm("employee.view_actiontype"),
            user.has_perm("base.view_tags"),
            user.has_perm("whatsapp.view_whatsappcredientials"),
            user.has_perm("base.view_company"),
            user.has_perm("base.view_tags"),
            user.has_perm("base.view_dynamicemailconfiguration"),
            user.has_perm("base.view_department"),
            user.has_perm("base.view_jobposition"),
            user.has_perm("base.view_jobrole"),
            user.has_perm("base.view_worktype"),
            user.has_perm("base.view_rotatingworktype"),
            user.has_perm("base.view_employeeshift"),
            user.has_perm("base.view_rotatingshift"),
            user.has_perm("base.view_employeeshiftschedule"),
            user.has_perm("base.view_employeetype"),
            user.has_perm("base.change_announcementexpire"),
            user.has_perm("base.view_dynamicpagination"),
            user.has_perm("joydigi_backup.view_googledrivebackup"),
            user.has_perm("recruitment.view_linkedinaccount"),
            user.has_perm("joydigi_ldap.add_ldapsettings"),
            user.has_perm("joydigi_ldap.update_ldapsettings"),
            user.has_perm("joydigi_meet.view_googlecloudcredential"),
            user.has_perm("whatsapp.add_whatsappcredientials"),
            user.has_perm("joydigi_theme.view_joydigicolortheme"),
        ]
    )


@register.simple_tag(takes_context=True)
def settings_menu(context):

    request = context.get("request")
    if request is None:
        return []
    return get_settings_menu(request)


@register.simple_tag
def settings_search_index():
    """
    Build the settings search index dynamically from the settings_registry.

    Each item in a sidebar class can declare a ``search_entries`` list of
    ``{"text": ..., "description": ...}`` dicts for field-level search
    granularity.  Items without ``search_entries`` fall back to a single
    page-level entry using the item label.

    To add new searchable fields for a settings page, open the relevant
    ``<app>/sidebar.py``, find the item dict, and add/extend its
    ``search_entries`` list — no changes needed anywhere else.
    """
    import json

    from joydigi.menu.settings_menu import settings_registry

    entries = []
    seen = set()  # (text_lower, url) pairs — prevents exact duplicates

    for cls in settings_registry._entries:
        obj = cls()
        section = str(getattr(obj, "title", ""))
        for item in getattr(obj, "items", []):
            page = str(item.get("label", ""))
            try:
                url = str(item.get("url", ""))
            except Exception:
                continue
            if not page or not url:
                continue

            search_entries = item.get("search_entries", [])
            if search_entries:
                for entry in search_entries:
                    text = str(entry.get("text", ""))
                    description = str(entry.get("description", ""))
                    key = (text.lower(), url)
                    if not text or key in seen:
                        continue
                    seen.add(key)
                    anchor = str(entry.get("anchor", ""))
                    entries.append(
                        {
                            "text": text,
                            "description": description,
                            "section": section,
                            "page": page,
                            "url": f"{url}#{anchor}" if anchor else url,
                        }
                    )
            else:
                # Fallback: page label only (no field-level granularity yet)
                key = (page.lower(), url)
                if key not in seen:
                    seen.add(key)
                    entries.append(
                        {
                            "text": page,
                            "description": "",
                            "section": section,
                            "page": page,
                            "url": url,
                        }
                    )

    return json.dumps(entries, ensure_ascii=False)


@register.filter(name="config_perms")
def config_perms(user):
    from django.apps import apps

    app_permissions = {
        "leave": ["leave.view_restrictleave"],
        "base": [
            "base.add_holidays",
            "base.change_holidays",
            "base.add_companyleaves",
            "base.change_companyleaves",
            "base.add_joydigimailtemplates",
            "base.view_joydigimailtemplates",
        ],
    }
    for app, perms in app_permissions.items():
        if apps.is_installed(app):
            for perm in perms:
                if user.has_perm(perm):
                    return True
    return False
