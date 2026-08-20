"""Ba vai trò cố định của hệ thống chấm công."""

from django.contrib.auth.models import Group
from django.db import DatabaseError, IntegrityError, transaction


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
