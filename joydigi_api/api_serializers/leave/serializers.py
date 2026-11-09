from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from employee.models import Employee
from leave.methods import calculate_requested_days
from leave.models import *


def leave_Validations(self, data):
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    start_date_breakdown = (
        data.get("start_date_breakdown")
        if data.get("start_date_breakdown") is not None
        else "full_day"
    )
    end_date_breakdown = (
        data.get("end_date_breakdown")
        if data.get("end_date_breakdown") is not None
        else "full_day"
    )
    employee = data.get("employee_id")
    leave_type_id = data.get("leave_type_id")
    attachment = data.get("attachment")
    # Who may request this type at all, said in words the employee can act
    # on (inactive type, another company, no work information, not opened).
    from leave.services import leave_request_problem

    problem = leave_request_problem(employee, leave_type_id)
    if problem:
        raise serializers.ValidationError(problem)
    available_leave = (
        AvailableLeave.objects.filter(
            leave_type_id=leave_type_id, employee_id=employee
        )[0]
        if AvailableLeave.objects.filter(
            leave_type_id=leave_type_id, employee_id=employee
        ).exists()
        else None
    )
    if not available_leave:
        raise serializers.ValidationError(
            _("Employee is not assigned with leave type %(leave_type)s.")
            % {"leave_type": leave_type_id}
        )

    requested_days = calculate_requested_days(
        start_date, end_date, start_date_breakdown, end_date_breakdown
    )
    effective_requested_days = cal_effective_requested_days(
        start_date=start_date,
        end_date=end_date,
        leave_type_id=leave_type_id,
        requested_days=requested_days,
    )

    total_leave_days = (
        available_leave.available_days + available_leave.carryforward_days
    )
    errors = {}
    # checking if there is any requested days is overlapping with the existing leave request
    # Rejected and cancelled requests no longer hold their dates (the web's
    # LeaveRequest.clean already excluded them).
    leave_requests = employee.leaverequest_set.filter(
        start_date__lte=end_date, end_date__gte=start_date
    ).exclude(status__in=["cancelled", "rejected"])
    if self.instance:
        leave_requests = leave_requests.exclude(id=self.instance.id)
    if leave_requests:
        raise serializers.ValidationError(
            _("There is already a leave request for this date range.")
        )

    # checking if the end date is less than the start date
    if not start_date <= end_date:
        errors["end_date"] = [_("End date should not be less than start date.")]

    if start_date == end_date and start_date_breakdown != end_date_breakdown:
        raise serializers.ValidationError(
            _("There is a mismatch in the breakdown of the start date and end date.")
        )

    if not effective_requested_days <= total_leave_days:
        raise serializers.ValidationError(
            _("Employee doesn't have enough leave days..")
        )

    if leave_type_id.require_attachment == "yes" and attachment == None:
        errors["attachment"] = [_("This field is required.")]

    if errors:
        raise serializers.ValidationError(errors)


class GetAvailableLeaveTypeSerializer(serializers.ModelSerializer):
    leave_type_id = serializers.SerializerMethodField()
    icon = serializers.SerializerMethodField()

    class Meta:
        model = AvailableLeave
        fields = [
            "id",
            "leave_type_id",
            "icon",
            "available_days",
            "carryforward_days",
            "total_leave_days",
        ]

    def get_leave_type_id(self, obj):
        if obj.leave_type_id:
            return LeaveTypeAllGetSerializer(obj.leave_type_id).data
        return None

    def get_icon(self, obj):
        try:
            return obj.leave_type_id.icon.url
        except:
            return None


class GetAvailableLeaveTypeSerializer(serializers.ModelSerializer):
    leave_type_id = serializers.SerializerMethodField()
    icon = serializers.SerializerMethodField()
    total_leave_days = serializers.SerializerMethodField()

    class Meta:
        model = AvailableLeave
        fields = [
            "id",
            "leave_type_id",
            "icon",
            "available_days",
            "carryforward_days",
            "total_leave_days",
        ]

    def get_leave_type_id(self, obj):
        if obj.leave_type_id:
            return LeaveTypeAllGetSerializer(obj.leave_type_id).data
        return None

    def get_icon(self, obj):
        try:
            return obj.leave_type_id.icon.url
        except:
            return None

    def get_total_leave_days(self, obj):
        return obj.available_days + obj.carryforward_days


class EmployeeLeaveOptionSerializer(GetAvailableLeaveTypeSerializer):
    """One requestable balance for the app: the keys app 1.0.1/1.0.2 read,
    plus remaining/granted days and a status the new app shows."""

    remaining_days = serializers.SerializerMethodField()
    granted_days = serializers.SerializerMethodField()
    limited = serializers.SerializerMethodField()
    requestable = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    status_message = serializers.SerializerMethodField()

    class Meta(GetAvailableLeaveTypeSerializer.Meta):
        fields = GetAvailableLeaveTypeSerializer.Meta.fields + [
            "remaining_days",
            "granted_days",
            "limited",
            "requestable",
            "status",
            "status_message",
        ]

    def get_remaining_days(self, obj):
        return (obj.available_days or 0) + (obj.carryforward_days or 0)

    def get_granted_days(self, obj):
        leave_type = obj.leave_type_id
        return leave_type.total_days if leave_type and leave_type.limit_leave else None

    def get_limited(self, obj):
        return bool(obj.leave_type_id and obj.leave_type_id.limit_leave)

    def _status(self, obj):
        from leave.services import leave_option_status

        return leave_option_status(obj)

    def get_requestable(self, obj):
        return self._status(obj)[1]

    def get_status(self, obj):
        return self._status(obj)[0]

    def get_status_message(self, obj):
        return self._status(obj)[2]


class UnassignedLeaveTypeSerializer(serializers.ModelSerializer):
    """An eligible type the employee has no balance for yet."""

    requestable = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    status_message = serializers.SerializerMethodField()

    class Meta:
        model = LeaveType
        fields = ["id", "name", "icon", "requestable", "status", "status_message"]

    def get_requestable(self, obj):
        return False

    def get_status(self, obj):
        return "not_assigned"

    def get_status_message(self, obj):
        from leave.services import NOT_ASSIGNED_SHORT

        return NOT_ASSIGNED_SHORT


class userLeaveRequestGetAllSerilaizer(serializers.ModelSerializer):
    leave_type_id = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        exclude = [
            "requested_date",
            "description",
            "attachment",
            "approved_available_days",
            "approved_carryforward_days",
            "created_at",
            "reject_reason",
            "employee_id",
            "created_by",
        ]

    def get_leave_type_id(self, obj):
        if obj.leave_type_id:
            return LeaveTypeAllGetSerializer(obj.leave_type_id).data
        return None


class UserLeaveRequestGetSerilaizer(serializers.ModelSerializer):
    leave_type_id = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        exclude = [
            "requested_date",
            "approved_available_days",
            "approved_carryforward_days",
            "created_at",
            "reject_reason",
            "employee_id",
            "created_by",
        ]

    def get_leave_type_id(self, obj):
        if obj.leave_type_id:
            return LeaveTypeAllGetSerializer(obj.leave_type_id).data
        return None


class LeaveRequestCreateUpdateSerializer(serializers.ModelSerializer):
    # Resolved without the request-time "active only" filter of the default
    # manager, so an inactive type reaches leave_request_problem() and the
    # employee reads "đã ngừng áp dụng" instead of DRF's "Invalid pk".
    leave_type_id = serializers.PrimaryKeyRelatedField(queryset=LeaveType._base_manager.all())

    class Meta:
        model = LeaveRequest
        fields = [
            "employee_id",
            "leave_type_id",
            "start_date",
            "start_date_breakdown",
            "end_date",
            "end_date_breakdown",
            "description",
            "attachment",
        ]

    def validate(self, data):
        leave_Validations(self, data)
        return data


class UpdateLeaveRequestSerializer(serializers.ModelSerializer):

    class Meta:
        model = LeaveRequest
        fields = [
            "start_date",
            "start_date_breakdown",
            "end_date",
            "end_date_breakdown",
            "description",
            "attachment",
        ]

    def validate(self, data):
        leave_Validations(self, data)
        return data


class LeaveTypeGetCreateSerilaizer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = "__all__"

    def validate(self, data):
        reset = data.get("reset")
        reset_based = data.get("reset_based")
        reset_month = data.get("reset_month")
        reset_day = data.get("reset_day")
        reset_weekday = data.get("reset_weekday")
        carryforward_type = data.get("carryforward_type")
        carryforward_max = data.get("carryforward_max")
        if reset == True:
            if reset_based == None:
                raise serializers.ValidationError(
                    {"reset_based": [_("This field is required.")]}
                )
            elif reset_based == "yearly" and reset_month == None:
                raise serializers.ValidationError(
                    {"reset_month": [_("This field is required.")]}
                )
            elif reset_based in ["yearly", "monthly"] and reset_day == "":
                raise serializers.ValidationError(
                    {"reset_day": [_("This field is required.")]}
                )
            elif reset_based == "weekly" and reset_weekday == None:
                raise serializers.ValidationError(
                    {"reset_weekday": [_("This field is required.")]}
                )
            # elif carryforward_type in ['carryforward', 'carryforward expire'] and carryforward_max
        return data


class LeaveTypeAllGetSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = ["id", "name", "icon"]


class LeaveAllocationRequestCreateSerializer(serializers.ModelSerializer):
    requested_days = serializers.FloatField(required=True)

    class Meta:
        model = LeaveAllocationRequest
        fields = [
            "leave_type_id",
            "employee_id",
            "requested_days",
            "created_by",
            "description",
            "attachment",
        ]


class AssignLeaveCreateSerializer(serializers.Serializer):
    leave_type_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=LeaveType.objects.all()
    )
    employee_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Employee.objects.all()
    )

    def validate_leave_type_ids(self, value):
        if not value:
            raise serializers.ValidationError(
                {"leave_type_ids": [_("This field is required.")]}
            )
        return value

    def validate_employee_ids(self, value):
        if not value:
            raise serializers.ValidationError(
                {"employee_ids": [_("This field is required.")]}
            )
        return value


class AssignLeaveGetSerializer(serializers.ModelSerializer):

    employee_id = serializers.SerializerMethodField()
    leave_type_id = serializers.SerializerMethodField()

    class Meta:
        model = AvailableLeave
        exclude = ["reset_date", "expired_date"]

    def get_employee_id(self, obj):
        employee = obj.employee_id
        if employee:
            return EmployeeGetSerializer(employee).data
        return None

    def get_leave_type_id(self, obj):
        if obj.leave_type_id:
            return LeaveTypeAllGetSerializer(obj.leave_type_id).data
        return None


class EmployeeGetSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = ["id", "full_name", "employee_profile", "badge_id"]

    def get_full_name(self, obj):
        return obj.get_full_name()


class AvailableLeaveUpdateSerializer(serializers.ModelSerializer):
    available_days = serializers.FloatField(required=True)

    class Meta:
        model = AvailableLeave
        fields = ["available_days", "carryforward_days"]


class LeaveRequestGetAllSerilaizer(serializers.ModelSerializer):
    employee_id = serializers.SerializerMethodField()
    leave_type_id = serializers.SerializerMethodField()
    multiple_approve = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        exclude = [
            "requested_date",
            "description",
            "attachment",
            "approved_available_days",
            "approved_carryforward_days",
            "created_at",
            "reject_reason",
            "created_by",
        ]

    def get_employee_id(self, obj):
        employee = obj.employee_id
        if employee:
            return EmployeeGetSerializer(employee).data
        return None

    def get_leave_type_id(self, obj):
        if obj.leave_type_id:
            return LeaveTypeAllGetSerializer(obj.leave_type_id).data
        return None

    def get_multiple_approve(self, obj):
        approvals = LeaveRequestConditionApproval.objects.filter(leave_request_id=obj)
        employee = self.context["request"].user.employee_get
        if approvals and obj.status == "requested":
            is_approved = approvals.filter(is_approved=True)
            count = f"{is_approved.count()} / {approvals.count()}"
            is_approved = (
                True if is_approved.filter(manager_id=employee).exists() else False
            )
            return {"count": count, "is_approved": is_approved}
        return None


class LeaveRequestGetSerilaizer(serializers.ModelSerializer):
    employee_id = serializers.SerializerMethodField()
    leave_type_id = serializers.SerializerMethodField()
    multiple_approve = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        exclude = [
            "requested_date",
            "approved_available_days",
            "approved_carryforward_days",
            "created_at",
            "reject_reason",
            "created_by",
        ]

    def get_employee_id(self, obj):
        employee = obj.employee_id
        if employee:
            return EmployeeGetSerializer(employee).data
        return None

    def get_leave_type_id(self, obj):
        if obj.leave_type_id:
            return LeaveTypeAllGetSerializer(obj.leave_type_id).data
        return None

    def get_multiple_approve(self, obj):
        approvals = LeaveRequestConditionApproval.objects.filter(leave_request_id=obj)
        employee = self.context["request"].user.employee_get
        if approvals and obj.status == "requested":
            is_approved = approvals.filter(is_approved=True)
            count = f"{is_approved.count()} / {approvals.count()}"
            is_approved = (
                True if is_approved.filter(manager_id=employee).exists() else False
            )
            return {"count": count, "is_approved": is_approved}
        return None


class LeaveAllocationRequestSerilaizer(serializers.ModelSerializer):
    class Meta:
        model = LeaveAllocationRequest
        exclude = [
            "requested_date",
            "status",
            "created_by",
            "created_at",
            "reject_reason",
        ]


class LeaveAllocationRequestGetSerializer(serializers.ModelSerializer):
    employee_id = serializers.SerializerMethodField()
    leave_type_id = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = LeaveAllocationRequest
        exclude = ["requested_date", "created_at", "reject_reason"]

    def get_employee_id(self, obj):
        employee = obj.employee_id
        if employee:
            return EmployeeGetSerializer(employee).data
        return None

    def get_leave_type_id(self, obj):
        if obj.leave_type_id:
            return LeaveTypeAllGetSerializer(obj.leave_type_id).data
        return None

    def get_created_by(self, obj):
        created_by = obj.created_by
        if created_by:
            return EmployeeGetSerializer(created_by).data
        return None


class CompanyLeaveSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyLeaves
        exclude = ["company_id"]


class HoildaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Holidays
        exclude = ["company_id"]

    def validate(self, data):
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        if end_date and not start_date <= end_date:
            raise serializers.ValidationError(
                {"end_date": [_("End date should not be less than start date.")]}
            )
        return data


class LeaveRequestApproveSerializer(serializers.ModelSerializer):

    class Meta:
        model = LeaveRequest
        fields = []

    def validate(self, data):
        leave_request = self.instance
        if leave_request.status != "requested":
            raise serializers.ValidationError(_("Nothing to approve."))
        employee_id = leave_request.employee_id
        leave_type_id = leave_request.leave_type_id
        available_leave = AvailableLeave.objects.get(
            leave_type_id=leave_type_id, employee_id=employee_id
        )
        total_available_leave = (
            available_leave.available_days + available_leave.carryforward_days
        )
        if not total_available_leave >= leave_request.requested_days:
            raise serializers.ValidationError(
                _("%(employee)s dont have enough leave days to approve the request..")
                % {"employee": employee_id}
            )
        data["available_leave"] = available_leave
        return data
