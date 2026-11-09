# leave/signals.py

import threading

from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_migrate, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from employee.models import Employee, EmployeeWorkInformation

from joydigi.methods import get_joydigi_model_class
from leave.models import (
    AvailableLeave,
    LeaveRequest,
    LeaveRequestConditionApproval,
    LeaveType,
    LeaveTypeCondition,
)

if apps.is_installed("attendance"):

    @receiver(post_save, sender=LeaveRequest)
    def leaverequest_pre_save(sender, instance, **_kwargs):
        """
        Overriding LeaveRequest model save method
        """
        WorkRecords = get_joydigi_model_class(
            app_label="attendance", model="workrecords"
        )
        if (
            instance.start_date == instance.end_date
            and instance.end_date_breakdown != instance.start_date_breakdown
        ):
            instance.end_date_breakdown = instance.start_date_breakdown
            super(LeaveRequest, instance).save()

        period_dates = instance.requested_dates()
        if instance.status == "approved":
            for date in period_dates:
                try:
                    work_entry = WorkRecords.objects.entire().filter(
                        date=date,
                        employee_id=instance.employee_id,
                    ).first() or WorkRecords()
                    work_entry.employee_id = instance.employee_id
                    work_entry.is_leave_record = True
                    work_entry.leave_request_id = instance
                    work_entry.day_percentage = (
                        0.50
                        if instance.start_date == date
                        and instance.start_date_breakdown == "first_half"
                        or instance.end_date == date
                        and instance.end_date_breakdown == "second_half"
                        else 0.00
                    )
                    status = (
                        "CONF"
                        if instance.start_date == date
                        and instance.start_date_breakdown == "first_half"
                        or instance.end_date == date
                        and instance.end_date_breakdown == "second_half"
                        else "ABS"
                    )
                    work_entry.work_record_type = status
                    work_entry.date = date
                    work_entry.message = (
                        "Leave"
                        if status == "ABS"
                        else _("Half day Attendance need to validate")
                    )
                    work_entry.save()

                except Exception as e:
                    print(e)

        else:
            for date in period_dates:
                WorkRecords._base_manager.filter(
                    is_leave_record=True,
                    date=date,
                    employee_id=instance.employee_id,
                ).delete()

    @receiver(pre_delete, sender=LeaveRequest)
    def leaverequest_pre_delete(sender, instance, **kwargs):
        from attendance.models import WorkRecords

        work_records = WorkRecords._base_manager.filter(
            leave_request_id=instance
        ).delete()


# @receiver(post_migrate)
def add_missing_leave_to_workrecords(sender, **kwargs):
    if sender.label not in ["attendance", "leave"]:
        return

    if not apps.is_installed("attendance"):
        return
    try:
        from attendance.models import WorkRecords
        from leave.models import LeaveRequest

        work_records = WorkRecords.objects.filter(
            is_leave_record=True, leave_request_id__isnull=True
        )
        if not work_records.exists():
            return

        leave_requests = LeaveRequest.objects.all()
        date_leave_map = {}

        for leave in leave_requests:
            for date in leave.requested_dates():
                key = (leave.employee_id, date)
                date_leave_map[key] = leave

        records_to_update = []
        for record in work_records:
            leave_request = date_leave_map.get((record.employee_id, record.date))
            if leave_request:
                record.leave_request_id = leave_request
                records_to_update.append(record)

        if records_to_update:
            WorkRecords.objects.bulk_update(
                records_to_update, ["leave_request_id"], batch_size=500
            )
            print(
                f"Successfully updated {len(records_to_update)} work records with leave information"
            )

    except Exception as e:
        print(f"Error in leave/work records sync: {e}")


@receiver(post_save, sender=LeaveType)
def assign_new_leave_type_to_employees(sender, instance, created, **kwargs):
    """A newly created leave type reaches its company's employees immediately.

    Hooked on the model rather than on a view because a leave type can be
    created from several places — the web form, the REST API, a data import —
    and every one of them must end up with the same assignments. Only the
    `created` case fires: editing an existing type must not silently re-open
    balances that an admin has deliberately removed.

    Work happens after the surrounding transaction commits, so a rolled-back
    leave-type creation cannot leave assignments behind.
    """
    if not created:
        return

    def _assign():
        from leave.services import assignable_employees, ensure_available_leave

        with transaction.atomic():
            for employee in assignable_employees(instance):
                ensure_available_leave(employee, instance)

    transaction.on_commit(_assign)


@receiver(post_save, sender=Employee)
def assign_leave_types_to_new_employee(sender, instance, created, **kwargs):
    """A new employee starts with every leave type their company offers.

    The mirror of `assign_new_leave_type_to_employees`. Note this fires on the
    Employee row itself, which is written before `EmployeeWorkInformation` —
    so at this point the employee usually has no company yet and only global
    (company-less) types match. `assign_leave_types_on_work_info` below closes
    that gap once the company is known.
    """
    if not created:
        return

    def _assign():
        from leave.services import assignable_leave_types, ensure_available_leave

        with transaction.atomic():
            for leave_type in assignable_leave_types(instance):
                ensure_available_leave(instance, leave_type)

    transaction.on_commit(_assign)


@receiver(post_save, sender=EmployeeWorkInformation)
def assign_leave_types_on_work_info(sender, instance, **kwargs):
    """Fill in the company-scoped types once an employee's company is known.

    An employee's company lives on their work information, which is saved
    after the employee itself and can be changed later. Runs on every save
    (not just `created`) so a transfer between companies also picks up the new
    company's types. `ensure_available_leave` is idempotent, so re-running
    costs nothing and never disturbs a balance that already exists — and
    nothing is removed here: taking a leave type away from someone stays a
    deliberate, manual act.
    """
    employee = instance.employee_id
    if employee is None:
        return

    def _assign():
        from leave.services import assignable_leave_types, ensure_available_leave

        with transaction.atomic():
            for leave_type in assignable_leave_types(employee):
                ensure_available_leave(employee, leave_type)

    transaction.on_commit(_assign)


@receiver(post_save, sender=LeaveRequestConditionApproval)
def auto_approve_self_approval_stage(sender, instance, created, **kwargs):
    """
    When an approver in the multiple-approval chain is the same employee who
    submitted the leave request, automatically approve their stage so the
    request is not stuck and can progress to the next approver.
    """
    if created and instance.manager_id == instance.leave_request_id.employee_id:
        sender.objects.filter(pk=instance.pk).update(is_approved=True)


# -- automatic sync on admin and employee changes (2026-11-09) ---------------

from django.db.models.signals import m2m_changed  # noqa: E402


@receiver(pre_save, sender=LeaveType)
def remember_leave_type_scope(sender, instance, **kwargs):
    """Keep what decided eligibility before this save (active, company)."""
    previous = None
    if instance.pk:
        previous = (
            LeaveType._base_manager.filter(pk=instance.pk).values_list("is_active", "company_id_id").first()
        )
    instance._sync_previous_scope = previous


@receiver(post_save, sender=LeaveType)
def sync_leave_type_when_scope_widens(sender, instance, created, **kwargs):
    """Re-activating a type, or moving it to another company / all companies,
    opens it for the employees who are now eligible. Creation is handled by
    ``assign_new_leave_type_to_employees``; editing the name or the days does
    nothing here, so a balance an admin removed on purpose is not re-opened
    and no existing balance is ever recomputed."""
    if created or not instance.is_active:
        return
    previous = getattr(instance, "_sync_previous_scope", None)
    was_active, old_company = previous if previous else (False, None)
    if was_active and old_company == instance.company_id_id:
        return

    def _sync():
        from leave.services import sync_leave_type

        with transaction.atomic():
            sync_leave_type(instance)

    transaction.on_commit(_sync)


@receiver(m2m_changed, sender=LeaveType.conditions.through)
def sync_leave_type_when_conditions_change(sender, instance, action, reverse, pk_set, **kwargs):
    """Changing a type's conditions can make more employees eligible."""
    if action == "pre_clear" and reverse:
        # Clearing from the condition's side passes no pk_set afterwards:
        # remember which types are about to lose this condition.
        instance._sync_cleared_leave_types = list(
            LeaveType._base_manager.filter(conditions=instance).values_list("pk", flat=True)
        )
        return
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if reverse:
        if action == "post_clear":
            pk_set = getattr(instance, "_sync_cleared_leave_types", None)
        leave_types = list(LeaveType._base_manager.filter(pk__in=pk_set or []))
    else:
        leave_types = [instance]

    def _sync():
        from leave.services import sync_leave_type

        with transaction.atomic():
            for leave_type in leave_types:
                sync_leave_type(leave_type)

    transaction.on_commit(_sync)


@receiver(post_save, sender=LeaveTypeCondition)
def sync_leave_types_when_condition_edited(sender, instance, created, **kwargs):
    """Editing a condition (its type or value) can make more employees
    eligible for every leave type that uses it."""
    if created:
        return
    leave_types = list(LeaveType._base_manager.filter(conditions=instance))
    if not leave_types:
        return

    def _sync():
        from leave.services import sync_leave_type

        with transaction.atomic():
            for leave_type in leave_types:
                sync_leave_type(leave_type)

    transaction.on_commit(_sync)


# Employee fields that decide eligibility: being active, and the ones
# evaluate_leave_type_conditions() reads from the employee itself (the
# department / employment type / grade ones live on the work information,
# whose own signal above already re-syncs on every save).
_EMPLOYEE_ELIGIBILITY_FIELDS = ("is_active", "gender", "marital_status", "country")


@receiver(pre_save, sender=Employee)
def remember_employee_eligibility(sender, instance, **kwargs):
    previous = None
    if instance.pk:
        previous = (
            Employee._base_manager.filter(pk=instance.pk).values_list(*_EMPLOYEE_ELIGIBILITY_FIELDS).first()
        )
    instance._sync_previous_eligibility = previous


@receiver(post_save, sender=Employee)
def sync_reactivated_employee(sender, instance, created, **kwargs):
    """An employee who is re-activated, or whose gender / marital status /
    nationality changes, gets every type they are now eligible for. Any
    other edit of the profile does nothing here, so a balance HR removed on
    purpose is not quietly re-opened by, say, a phone-number change. New
    employees are handled by ``assign_leave_types_to_new_employee``."""
    if created or not instance.is_active:
        return
    current = tuple(getattr(instance, field) for field in _EMPLOYEE_ELIGIBILITY_FIELDS)
    if getattr(instance, "_sync_previous_eligibility", None) == current:
        return

    def _sync():
        from leave.services import sync_employee

        with transaction.atomic():
            sync_employee(instance)

    transaction.on_commit(_sync)
