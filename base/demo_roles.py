"""
Demo-data helpers for role (user group) assignments.

Groups are created by post_migrate (see base.signals._DEFAULT_HRMS_GROUPS),
so membership is applied by group *name* after fixtures load — never by PK.
"""

from __future__ import annotations

import logging

from django.contrib.auth.models import Group
from django.db import transaction

from base.models import Company, CompanyGroupAssignment
from joydigi_auth.models import JoydigiUser

logger = logging.getLogger(__name__)

# (user email, group name, company name)
# Keep this small — enough to demo Roles & Permissions and company-scoped access.
DEMO_ROLE_ASSIGNMENTS = (
    ("huynh.duy.thu@joydigi.com", "Nhân viên", "Công ty của bạn"),
    ("huynh.duy.thu@joydigi.com", "Nhân viên", "Công ty của bạn Inc."),
    ("huynh.anh.hai@joydigi.com", "Trưởng nhóm", "Công ty của bạn"),
    ("dinh.thanh.tu@joydigi.com", "Nhân viên", "Công ty của bạn"),
    ("ta.huu.diem@joydigi.com", "Trưởng nhóm", "Công ty của bạn"),
    ("vo.nhat.khang@joydigi.com", "Trưởng nhóm", "Công ty của bạn"),
    ("huynh.gia.an@joydigi.com", "Nhân viên", "Công ty của bạn Ltd."),
    ("nguyen.anh.ngoc@joydigi.com", "Nhân viên", "Công ty của bạn Inc."),
    ("thai.huu.dung@joydigi.com", "Nhân viên", "Công ty của bạn"),
    ("duong.anh.phong@joydigi.com", "Nhân viên", "Công ty của bạn"),
)


@transaction.atomic
def assign_demo_user_groups():
    """
    Assign a few demo employees to default HRMS roles.

    Safe to call repeatedly (get_or_create). Skips missing users/groups/companies.
    Syncs ``user.groups`` so legacy (non-scoped) mode still works.
    """
    created = 0
    for email, group_name, company_name in DEMO_ROLE_ASSIGNMENTS:
        user = JoydigiUser.objects.filter(email=email).first()
        if not user:
            user = JoydigiUser.objects.filter(username=email).first()
        group = Group.objects.filter(name=group_name).first()
        company = Company.objects.filter(company=company_name).first()
        if not user or not group or not company:
            logger.debug(
                "Skipping demo role assignment %s / %s / %s (missing user=%s group=%s company=%s)",
                email,
                group_name,
                company_name,
                bool(user),
                bool(group),
                bool(company),
            )
            continue
        _, was_created = CompanyGroupAssignment.objects.get_or_create(
            user=user,
            group=group,
            company=company,
        )
        CompanyGroupAssignment.sync_user_group_membership(user, group)
        if was_created:
            created += 1
    return created
