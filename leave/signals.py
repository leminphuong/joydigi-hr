# leave/signals.py

import threading

from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_migrate, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from employee.models import Employee, EmployeeWorkInformation

from joydigi.methods import get_joydigi_model_class
from leave.models import AvailableLeave, LeaveRequest, LeaveRequestConditionApproval, LeaveType

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
