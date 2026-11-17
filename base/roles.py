"""Các vai trò cố định của hệ thống chấm công.

Ba vai trò gốc được nhận diện theo tên nhóm: Quản trị viên, Trưởng nhóm,
Nhân viên.

Vai trò Giám đốc (2026-11-17) KHÔNG được nhận diện theo tên. Nhóm Giám đốc là
nhóm mang quyền đánh dấu ``base.director_role``, do migration
``base 0017_director_role`` tạo ra (rỗng, không có thành viên). Vì vậy một
nhóm tên "Giám đốc" do ai đó tự tạo từ trước không bao giờ tự trở thành vai
trò Giám đốc: muốn thế, superuser phải tự gắn quyền đánh dấu cho nhóm đó.

Giám đốc có toàn bộ quyền nghiệp vụ của Quản trị viên trong công ty được gán
(``is_checkin_admin`` và các kiểm tra theo công ty đều nhận Giám đốc), cộng
quyền duyệt thay bước hiện tại của đơn nghỉ (``leave.request_access``).
Giám đốc không phải superuser, không phải staff. Chỉ superuser được cấp hoặc
thu hồi vai trò này (``role_grant_problem``).
"""

from functools import wraps

from django.contrib.auth.models import Group
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponseForbidden


ADMIN_ROLE = "Quản trị viên"
LEADER_ROLE = "Trưởng nhóm"
EMPLOYEE_ROLE = "Nhân viên"
STANDARD_ROLE_NAMES = (ADMIN_ROLE, LEADER_ROLE, EMPLOYEE_ROLE)

# Giám đốc: tên hiển thị mặc định của nhóm do migration tạo. Việc nhận diện
# chỉ dựa vào quyền đánh dấu bên dưới, không dựa vào tên.
DIRECTOR_ROLE = "Giám đốc"
DIRECTOR_MARKER_APP_LABEL = "base"
DIRECTOR_MARKER_CODENAME = "director_role"


def ensure_standard_roles():
    """Tạo các vai trò gốc còn thiếu mà không đụng đến dữ liệu phân quyền cũ.

    Không tạo nhóm Giám đốc: nhóm đó chỉ do migration tạo ra."""
    try:
        with transaction.atomic():
            for name in STANDARD_ROLE_NAMES:
                Group.objects.get_or_create(name=name)
    except (DatabaseError, IntegrityError):
        # Cho phép lệnh kiểm tra/migration chạy khi bảng auth chưa sẵn sàng.
        return


def director_group_q(prefix=""):
    """Điều kiện lọc nhóm Giám đốc: theo quyền đánh dấu, không theo tên.

    Ba nhóm vai trò gốc không bao giờ là nhóm Giám đốc, kể cả khi quyền đánh
    dấu lỡ bị gắn vào chúng (``post_migrate`` thêm mọi quyền của app ``base``
    cho Quản trị viên; quyền đánh dấu được loại khỏi việc đó, đây là lớp chặn
    thứ hai)."""
    return Q(
        **{
            f"{prefix}permissions__codename": DIRECTOR_MARKER_CODENAME,
            f"{prefix}permissions__content_type__app_label": DIRECTOR_MARKER_APP_LABEL,
        }
    ) & ~Q(**{f"{prefix}name__in": STANDARD_ROLE_NAMES})


def admin_level_group_q(prefix=""):
    """Nhóm Quản trị viên hoặc Giám đốc."""
    return Q(**{f"{prefix}name": ADMIN_ROLE}) | director_group_q(prefix)


def standard_role_q(prefix=""):
    return Q(**{f"{prefix}name__in": STANDARD_ROLE_NAMES}) | director_group_q(prefix)


def director_groups():
    return Group.objects.filter(director_group_q()).distinct()


def director_marker_permission():
    """The marker permission row (created by migration ``base 0017``)."""
    from django.contrib.auth.models import Permission

    return Permission.objects.filter(
        content_type__app_label=DIRECTOR_MARKER_APP_LABEL,
        codename=DIRECTOR_MARKER_CODENAME,
    ).first()


def keep_director_marker(group, was_director):
    """After a superuser edits a role's permissions: the Director group keeps
    its marker, and no other group gets it from the permission matrix."""
    marker = director_marker_permission()
    if marker is None or group is None:
        return
    if was_director:
        group.permissions.add(marker)
    else:
        group.permissions.remove(marker)


def standard_roles_queryset():
    ensure_standard_roles()
    return Group.objects.filter(
        id__in=Group.objects.filter(standard_role_q()).values("id")
    )


def is_director_group(group) -> bool:
    if group is None or not getattr(group, "pk", None):
        return False
    return Group.objects.filter(pk=group.pk).filter(director_group_q()).exists()


def is_admin_level_group(group) -> bool:
    return bool(group) and (group.name == ADMIN_ROLE or is_director_group(group))


def is_standard_role(group):
    return bool(group) and (group.name in STANDARD_ROLE_NAMES or is_director_group(group))


def _groups_for(user, company_id=None):
    from base.auth_backends import get_user_groups_for_company

    return get_user_groups_for_company(user, company_id)


def user_has_role(user, role_name):
    """Kiểm tra vai trò theo phạm vi công ty hiện tại của người dùng."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    try:
        return _groups_for(user).filter(name=role_name).exists()
    except (DatabaseError, AttributeError):
        return user.groups.filter(name=role_name).exists()


def user_has_director_role(user, company_id=None):
    """Có vai trò Giám đốc (ở ``company_id``, hoặc ở ít nhất một công ty)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    try:
        return _groups_for(user, company_id).filter(director_group_q()).exists()
    except (DatabaseError, AttributeError):
        return user.groups.filter(director_group_q()).exists()


def holds_admin_level_role(user, company_id=None):
    """Có vai trò Quản trị viên hoặc Giám đốc (ở ``company_id``, hoặc hợp các
    công ty khi ``company_id`` là None)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    try:
        return _groups_for(user, company_id).filter(admin_level_group_q()).exists()
    except (DatabaseError, AttributeError):
        return user.groups.filter(admin_level_group_q()).exists()


def is_checkin_director(user, company_id=None):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and user_has_director_role(user, company_id)
    )


def is_checkin_admin(user):
    """Superuser, Quản trị viên hoặc Giám đốc (Giám đốc có toàn bộ quyền nghiệp
    vụ của Quản trị viên)."""
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or holds_admin_level_role(user))
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


def can_open_approval_hub(user):
    """Trang Duyệt đơn (2026-11-17): Trưởng nhóm, Quản trị viên, Giám đốc, hoặc
    người đang được giao một bước duyệt của đơn nghỉ đang chờ — dù được chọn
    vì là quản lý trực tiếp hay theo Điều kiện duyệt nhiều cấp. Menu, số đơn
    chờ và view đều dùng hàm này."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_checkin_leader(user):
        return True
    from leave.request_access import has_any_step

    # Also once their last step is decided: the hub shows them the history
    # of the leaves they approve (and the page they are sent back to after a
    # decision must not turn into a 403).
    return has_any_step(user)


def role_grant_problem(actor, groups, target_users=(), *, all_companies=False):
    """None khi ``actor`` được gán hoặc gỡ ``groups`` của ``target_users``,
    còn không thì lý do (2026-11-17, P0): chỉ superuser.

    * Không có người dùng đăng nhập (lệnh quản trị, migration, shell): không
      chặn — các view gán vai trò đều đã yêu cầu superuser.
    * Superuser: được.
    * Mọi người khác, kể cả Quản trị viên và Giám đốc: không. Lý do cụ thể
      (tự đổi vai trò, vai trò Quản trị viên/Giám đốc, mọi công ty) chỉ để
      thông báo rõ hơn."""
    if actor is None or not getattr(actor, "is_authenticated", False):
        return None
    if getattr(actor, "is_superuser", False):
        return None
    if any(getattr(user, "pk", None) == actor.pk for user in target_users if user):
        return "Bạn không thể tự thay đổi vai trò của chính mình."
    if any(is_admin_level_group(group) for group in groups if group):
        return "Chỉ superuser được cấp vai trò Quản trị viên hoặc Giám đốc."
    if all_companies:
        return "Chỉ superuser được gán vai trò ở mọi công ty."
    return "Chỉ superuser được gán hoặc gỡ vai trò."


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
        if not user.groups.filter(standard_role_q()).exists():
            user.groups.add(employee_group)
            return True
        return False

    from base.models import CompanyGroupAssignment

    has_role = CompanyGroupAssignment.objects.filter(
        Q(user=user, company=company) & standard_role_q("group__")
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
approval_hub_access_required = _role_required(
    can_open_approval_hub, "Bạn không có quyền duyệt đơn."
)
