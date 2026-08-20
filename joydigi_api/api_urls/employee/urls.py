from django.urls import path

from ...api_views.employee import views as views

urlpatterns = [
    # path('employees/', views.EmployeeAPIView.as_view(), name='api-employees-list'),
    path(
        "employees/<int:pk>/",
        views.EmployeeAPIView.as_view(),
        name="api-employee-detail",
    ),
    path(
        "employee-type/<int:pk>",
        views.EmployeeTypeAPIView.as_view(),
        name="api-employees",
    ),
    path("employee-type/", views.EmployeeTypeAPIView.as_view(), name="api-employees"),
    path(
        "list/employees/",
        views.EmployeeListAPIView.as_view(),
        name="api-employee-list-detailed",
    ),  # Alternative endpoint for listing employees
    path(
        "employee-bank-details/<int:pk>/",
        views.EmployeeBankDetailsAPIView.as_view(),
        name="api-employee-bank-details-detail",
    ),
    path(
        "employee-work-information/",
        views.EmployeeWorkInformationAPIView.as_view(),
        name="api-employee-work-information-list",
    ),
    path(
        "employee-work-information/<int:pk>/",
        views.EmployeeWorkInformationAPIView.as_view(),
        name="api-employee-work-information-detail",
    ),
    path(
        "employee-work-info-export/",
        views.EmployeeWorkInfoExportView.as_view(),
        name="api-employee-work-info-export",
    ),
    path(
        "employee-work-info-import/",
        views.EmployeeWorkInfoImportView.as_view(),
        name="api-employee-work-info-import",
    ),
    path(
        "employee-bulk-update/",
        views.EmployeeBulkUpdateView.as_view(),
        name="api-employee-bulk-update",
    ),
    path(
        "employee-bulk- archive/<str:is_active>/",
        views.EmployeeBulkArchiveView.as_view(),
        name="api-employee-bulk-archive",
    ),
    path(
        "employee-archive/<int:id>/<str:is_active>/",
        views.EmployeeArchiveView.as_view(),
        name="api-employee-archive",
    ),
    path(
        "employee-selector/",
        views.EmployeeSelectorView.as_view(),
        name="api-employee-selector",
    ),
    path(
        "manager-check/",
        views.ReportingManagerCheck.as_view(),
        name="api-manager-check",
    ),
]
