from collections import Counter

from django.db.models import Q
from django.http import QueryDict
from rest_framework.pagination import PageNumberPagination

from employee.models import EmployeeWorkInformation


def get_filter_url(current_url, request):
    url_parts = current_url.split("?")
    base_url = request.path
    query_params = QueryDict(url_parts[1], mutable=True)
    query_params.pop("groupby_field", None)
    return base_url + "?" + query_params.urlencode()


#: Nested group-by paths that are safe to expose: organisation labels only.
SAFE_GROUPBY_PATHS = frozenset({
    "employee_id__employee_work_info__department_id",
    "employee_id__employee_work_info__job_position_id",
    "employee_id__employee_work_info__job_role_id",
    "employee_id__employee_work_info__company_id",
    "employee_id__employee_work_info__shift_id",
    "employee_id__employee_work_info__work_type_id",
    "employee_id__employee_work_info__employee_type_id",
    "employee_id__employee_work_info__reporting_manager_id",
})

#: Field names never offered for grouping, whatever the model.
_SENSITIVE_GROUPBY_HINTS = (
    "password", "token", "secret", "key", "account", "bank", "salary",
    "email", "phone", "mobile", "dob", "address", "embedding", "requested_data",
)


def allowed_groupby_field(model, field_name):
    """`groupby_field` comes straight from the query string (2026-11-13).

    It used to reach `queryset.values()` unchecked, so any ORM path worked —
    `employee_id__employee_work_info__reporting_manager_id__employee_user_id__password`
    read the manager's password hash. Only a concrete field of the model
    itself (not a sensitive one) or one of SAFE_GROUPBY_PATHS is allowed.
    """
    from django.core.exceptions import FieldDoesNotExist

    if not isinstance(field_name, str) or not field_name:
        return False
    if field_name in SAFE_GROUPBY_PATHS:
        return True
    if "__" in field_name or not field_name.isidentifier():
        return False
    if any(hint in field_name.lower() for hint in _SENSITIVE_GROUPBY_HINTS):
        return False
    try:
        field = model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return False
    return bool(getattr(field, "concrete", False))


def groupby_queryset(request, url, field_name, queryset):
    from django.core.exceptions import FieldError
    from rest_framework.response import Response

    if not allowed_groupby_field(queryset.model, field_name):
        return Response({"groupby_field": ["This field can not be used for grouping."]}, status=400)
    try:
        queryset_with_counts = list(queryset.values(field_name))
    except FieldError:
        return Response({"groupby_field": ["This field can not be used for grouping."]}, status=400)

    counts = Counter(item[field_name] for item in queryset_with_counts)

    result_list = []
    for i in counts:
        result_list.append({field_name: i, "count": counts[i]})

    counts_and_objects = []
    url = get_filter_url(url, request)
    for item in result_list:
        count = item["count"]
        related_fields = field_name.split("__")
        if item[field_name]:
            related_obj = queryset.filter(**{field_name: item[field_name]}).first()
            for field in related_fields:
                related_obj = getattr(related_obj, field)
            counts_and_objects.append(
                {
                    "count": count,
                    "name": str(related_obj),
                    "filter_url": f"{url}&{field_name}={item[field_name]}",
                }
            )
    pagination = PageNumberPagination()
    page = pagination.paginate_queryset(counts_and_objects, request)
    return pagination.get_paginated_response(page)


def permission_based_queryset(user, perm, queryset, user_obj=None):
    # Handle AnonymousUser during schema generation
    if not user.is_authenticated:
        return queryset.none()

    if user.has_perm(perm):
        return queryset

    employee = user.employee_get
    is_manager = EmployeeWorkInformation.objects.filter(
        reporting_manager_id=employee
    ).exists()
    if is_manager:
        if user_obj:
            return queryset.filter(
                Q(employee_id=employee)
                | Q(employee_id__employee_work_info__reporting_manager_id=employee)
            )
        manager_filter = Q(employee_id=employee)
        subordinates_filter = Q(
            employee_id__employee_work_info__reporting_manager_id=employee
        )
        merged_filter = manager_filter | subordinates_filter
        merged_queryset = queryset.filter(merged_filter)
        return merged_queryset

    return queryset.filter(employee_id=employee)
