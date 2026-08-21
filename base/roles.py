"""Ba vai trò cố định của hệ thống chấm công."""

from django.contrib.auth.models import Group
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpResponseForbidden
from functools import wraps


ADMIN_ROLE = "Quản trị viên"
LEADER_ROLE = "Trưởng nhóm"
EMPLOYEE_ROLE = "Nhân viên"
STANDARD_ROLE_NAMES = (ADMIN_ROLE, LEADER_ROLE, EMPLOYEE_ROLE)


def ensure_standard_roles():
    """Tạo các vai trò còn thiếu mà không đụng đến dữ liệu phân quyền cũ."""
    try:
        with transaction.atomic():
            for name in STANDARD_ROLE_NAMES:
                Group.objects.get_or_create(name=name)
    except (DatabaseError, IntegrityError):
        # Cho phép lệnh kiểm tra/migration chạy khi bảng auth chưa sẵn sàng.
        return


def standard_roles_queryset():
    ensure_standard_roles()
    return Group.objects.filter(name__in=STANDARD_ROLE_NAMES)


def is_standard_role(group):
    return bool(group and group.name in STANDARD_ROLE_NAMES)


def user_has_role(user, role_name):
    """Kiểm tra vai trò theo phạm vi công ty hiện tại của người dùng."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    try:
        from base.auth_backends import get_user_groups_for_company

        return get_user_groups_for_company(user).filter(name=role_name).exists()
    except (DatabaseError, AttributeError):
        return user.groups.filter(name=role_name).exists()


def is_checkin_admin(user):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or user_has_role(user, ADMIN_ROLE))
    )


def is_checkin_leader(user):
    """Quản trị viên cũng có toàn bộ quyền của trưởng nhóm."""
    return is_checkin_admin(user) or user_has_role(user, LEADER_ROLE)


def is_checkin_employee(user):
    """Mọi tài khoản nhân sự đều có quyền tự phục vụ cá nhân."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return bool(
        is_checkin_leader(user)
        or user_has_role(user, EMPLOYEE_ROLE)
        or getattr(user, "employee_get", None)
    )


def assign_default_employee_role(employee, company=None):
    """Gán vai trò Nhân viên nếu tài khoản chưa có vai trò tại công ty."""
    user = getattr(employee, "employee_user_id", None)
    if user is None:
        return False
    if getattr(user, "is_superuser", False):
        return False

    ensure_standard_roles()
    employee_group = Group.objects.filter(name=EMPLOYEE_ROLE).first()
    if employee_group is None:
        return False

    if company is None:
        company = getattr(
            getattr(employee, "employee_work_info", None), "company_id", None
        )

    if company is None:
        if not user.groups.filter(name__in=STANDARD_ROLE_NAMES).exists():
            user.groups.add(employee_group)
            return True
        return False

    from base.models import CompanyGroupAssignment

    has_role = CompanyGroupAssignment.objects.filter(
        user=user,
        company=company,
        group__name__in=STANDARD_ROLE_NAMES,
    ).exists()
    if has_role:
        return False

    _, created = CompanyGroupAssignment.objects.get_or_create(
        user=user,
        company=company,
        group=employee_group,
    )
    CompanyGroupAssignment.sync_user_group_membership(user, employee_group)
    return created


def _role_required(checker, message):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not checker(request.user):
                return HttpResponseForbidden(message)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


checkin_admin_required = _role_required(
    is_checkin_admin, "Bạn không có quyền quản trị hệ thống chấm công."
)
checkin_leader_required = _role_required(
    is_checkin_leader, "Bạn không có quyền quản lý chấm công của nhóm."
)
