"""`POST /api/internal/attendance/update-office-ip/`.

Phase AUTO-OFFICE-PUBLIC-IP-UPDATER. See `joydigi_api.office_ip` for the
rules; this module is only the HTTP edge.

Not a user endpoint. It carries no JWT, no session and no employee: a normal
mobile token is worth nothing here, because the only credential it accepts is
an HMAC signature made with a secret that lives in the deployment
environment. That is deliberate — whitelisting a network is an
infrastructure act, and no employee's login should be able to perform it.
"""

import logging

from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from attendance.methods.client_ip import resolve_attendance_client_ip
from base.models import AttendanceAllowedIP, Company
from joydigi_api.office_ip import (
    UNAUTHORIZED,
    apply_office_ip,
    as_single_host_cidr,
    configured_company_id,
    is_configured,
    mask,
    verify_request,
)

logger = logging.getLogger(__name__)


class UpdateOfficeIPView(APIView):
    """Record the address this request came from as the office's address.

    POST only, and every other method gets 405 from DRF without running any
    of this.
    """

    # No authentication at all, on purpose: DRF must not try to read a JWT or
    # a session here, and `AllowAny` means the HMAC check below is the only
    # gate. It runs before anything is read or written.
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not is_configured():
            # Unset secret or unset company. Answering 404 rather than 403
            # keeps a disabled feature from advertising that it exists.
            logger.info("OFFICE_IP_UPDATE_DISABLED: feature is not configured")
            return Response(status=404)

        ok, reason = verify_request(request)
        if not ok:
            # The reason goes to the log, never to the caller.
            logger.warning("OFFICE_IP_UPDATE_REFUSED: %s", reason)
            return Response({"code": UNAUTHORIZED}, status=401)

        company = Company.objects.filter(pk=configured_company_id()).first()
        if company is None:
            logger.error(
                "OFFICE_IP_UPDATE_MISCONFIGURED: OFFICE_IP_UPDATER_COMPANY_ID "
                "names no company"
            )
            return Response(status=404)

        # The address is taken from the request, and the body is never read —
        # not even to validate it. An agent that sends `{"ip": ...}` is
        # ignored, which is the whole point of resolving it here.
        new_cidr = as_single_host_cidr(resolve_attendance_client_ip(request))
        if new_cidr is None:
            logger.warning(
                "OFFICE_IP_UPDATE_NO_CLIENT_IP: the request carried no "
                "trustworthy address"
            )
            return Response({"code": "CLIENT_IP_UNRESOLVED"}, status=400)

        rule = AttendanceAllowedIP.objects.filter(company_id=company).first()
        created_disabled = False
        if rule is None:
            # A company with no rule yet gets one, switched OFF. Creating an
            # enabled rule here would start refusing every employee who is
            # not on this one address, from a background agent, with nobody
            # having decided to turn the restriction on.
            rule = AttendanceAllowedIP(
                company_id=company, is_enabled=False, additional_data={"allowed_ips": []}
            )
            created_disabled = True

        data, outcome = apply_office_ip(rule.additional_data, new_cidr)
        rule.additional_data = data
        # `is_enabled` is never written: this feature maintains the list, it
        # does not decide whether the restriction applies.
        rule.save()

        state = data.get("office_ip_auto") or {}
        logger.info(
            "OFFICE_IP_UPDATE_%s: company=%s current=%s previous=%s enabled=%s",
            outcome.upper(),
            company.pk,
            mask(state.get("current")),
            mask(state.get("previous")),
            rule.is_enabled,
        )

        return Response(
            {
                "status": outcome,
                # Masked: the agent has no use for the value, and a response
                # body is one more place an address could be copied out of.
                "current": mask(state.get("current")),
                "previous_retained": bool(state.get("previous")),
                "rule_enabled": rule.is_enabled,
                "created_disabled": created_disabled,
            },
            status=200,
        )
