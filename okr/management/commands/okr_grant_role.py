"""Grant (or revoke) an OKR role from the command line.

This is how a company gets its first OKR admin: the roles API itself is
admin-only. Nothing is hard-coded; the operator names the employee and the
HR account authorising the change, and the command checks both:

    python manage.py okr_grant_role --employee <id|e-mail|employee code> \\
        --role admin --granted-by <HR username>            # dry run
    python manage.py okr_grant_role ... --yes               # apply
    python manage.py okr_grant_role ... --revoke --yes      # remove

The authoriser must be an active superuser, or an active HR administrator
("Quản trị viên") of the employee's own company.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from okr import pilot
from okr.models import OKRRole
from okr.models.base import employee_company_id
from okr.models.choices import OKRRoleType


def _resolve_employee(value: str):
    from employee.models import Employee

    rows = Employee._base_manager.select_related("employee_user_id")
    if value.isdigit():
        matches = list(rows.filter(pk=int(value)))
    else:
        matches = list(rows.filter(Q(email__iexact=value) | Q(badge_id__iexact=value)))
    if len(matches) != 1:
        raise CommandError("Không tìm thấy đúng một nhân viên khớp với --employee.")
    return matches[0]


def _may_authorise(user, company_id) -> bool:
    from base.roles import ADMIN_ROLE

    if not user.is_active:
        return False
    if user.is_superuser:
        return True
    employee = getattr(user, "employee_get", None)
    same_company = employee is not None and employee_company_id(employee) == company_id
    return same_company and user.groups.filter(name=ADMIN_ROLE).exists()


class Command(BaseCommand):
    help = "Grant or revoke an OKR role (manager/admin) for one employee, authorised by an HR admin account."

    def add_arguments(self, parser):
        parser.add_argument("--employee", required=True, help="Employee id, e-mail or employee code.")
        parser.add_argument("--role", required=True, choices=OKRRoleType.values)
        parser.add_argument("--granted-by", required=True, help="Username of the authorising HR account.")
        parser.add_argument("--revoke", action="store_true", help="Remove the role instead of granting it.")
        parser.add_argument("--yes", action="store_true", help="Apply the change (otherwise only a dry run).")

    def handle(self, *args, **options):
        employee = _resolve_employee(options["employee"].strip())
        user = employee.employee_user_id
        if not employee.is_active or user is None or not user.is_active:
            raise CommandError("Nhân viên không còn hoạt động hoặc chưa có tài khoản đăng nhập.")
        company_id = employee_company_id(employee)
        if company_id is None:
            raise CommandError("Nhân viên chưa thuộc công ty nào.")
        authoriser = get_user_model().objects.filter(username=options["granted_by"]).first()
        if authoriser is None or not _may_authorise(authoriser, company_id):
            raise CommandError(
                "--granted-by phải là superuser hoặc Quản trị viên HR đang hoạt động của cùng công ty."
            )
        role = options["role"]
        existing = OKRRole.objects.for_company(company_id).filter(employee=employee, role=role).first()
        action = "Gỡ" if options["revoke"] else "Cấp"
        self.stdout.write(
            f"{action} vai trò OKR '{role}' cho nhân viên #{employee.pk} ({employee.get_full_name()}), "
            f"công ty #{company_id}, người duyệt: {authoriser.username}."
        )
        if options["revoke"]:
            if existing is None:
                raise CommandError("Nhân viên chưa có vai trò này.")
            admins = OKRRole.objects.for_company(company_id).filter(role=OKRRoleType.ADMIN).count()
            if role == OKRRoleType.ADMIN and admins <= 1:
                raise CommandError("Không thể gỡ quản trị OKR cuối cùng của công ty.")
        elif existing is not None:
            raise CommandError("Nhân viên đã có vai trò này.")
        elif not pilot.allows(employee.pk):
            # Pilot mode: a role for someone outside the list could never be used.
            raise CommandError(pilot.NOT_IN_PILOT + " Thêm vào OKR_PILOT_EMPLOYEE_IDS trước.")
        if not options["yes"]:
            self.stdout.write("Chạy thử: chưa thay đổi gì. Thêm --yes để áp dụng.")
            return
        with transaction.atomic():
            if options["revoke"]:
                existing.delete()
            else:
                OKRRole.objects.create(company_id=company_id, employee=employee, role=role, granted_by=authoriser)
        self.stdout.write(self.style.SUCCESS("Đã áp dụng."))
