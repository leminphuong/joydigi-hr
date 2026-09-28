"""
Announcement page
"""

import logging
from functools import partial

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.urls import resolve, reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from base.forms import AnnouncementForm
from base.methods import closest_numbers
from base.models import Announcement, AnnouncementView
from employee.models import Employee
from joydigi.http.response import JoydigiRedirect
from joydigi_auth.models import JoydigiUser
from joydigi_views.cbv_methods import login_required, permission_required
from joydigi_views.generic.cbv.views import (
    JoydigiDetailedView,
    JoydigiFormView,
    JoydigiListView,
)
from notifications.signals import notify

logger = logging.getLogger(__name__)

#: What the lock screen shows. The body is the post's own title, and the
#: description deliberately never goes into a push: it can be arbitrarily
#: long, and the notification only has to get the employee to open the app.
ANNOUNCEMENT_PUSH_TITLE = "Thông báo mới từ JOYDIGI"


def announcement_notifications(announcement):
    """Every in-app notification already recorded for this announcement.

    Reading the rows back is what makes both dedupe and push honest.
    `notify.send` silently drops recipients who turned notifications off
    (see `notifications.base.models.notify_handler`), so the audience is
    not the same thing as "who was told" — and pushing to the difference
    would ring a phone for a notification that is not in the app.
    """
    from notifications.models import Notification

    return Notification.objects.filter(
        action_object_content_type=ContentType.objects.get_for_model(
            announcement.__class__, for_concrete_model=True
        ),
        # The generic-FK id column is a CharField, so the pk has to be
        # compared as text or nothing matches.
        action_object_object_id=str(announcement.pk),
    )


def push_new_announcement(announcement_pk, announcement_title, user_ids):
    """Push a new bulletin to the devices of users who were notified.

    Runs from `transaction.on_commit`, so the row it announces is
    certainly committed. Best effort throughout, exactly like the
    attendance reminders it reuses: a device that cannot be reached, a
    deployment with no Firebase credential, a dead token — none of them
    may undo the post or stop the next employee being reached. No token
    value is ever logged, only the device owner and a status.
    """
    from joydigi_api.push import send_to_user

    for user in JoydigiUser.objects.filter(pk__in=list(user_ids)):
        try:
            tally = send_to_user(
                user,
                ANNOUNCEMENT_PUSH_TITLE,
                announcement_title,
                data={"type": "NEWS", "post_id": str(announcement_pk)},
            )
        except Exception as error:
            logger.warning(
                "announcement push failed for user %s: %s", user.pk, error
            )
            continue
        logger.info(
            "announcement push for user %s: %s", user.pk, tally.get("status")
        )


@method_decorator(login_required, name="dispatch")
@method_decorator(permission_required(perm="base.add_announcement"), name="dispatch")
class AnnouncementFormView(JoydigiFormView):
    """
    form view for create button
    """

    form_class = AnnouncementForm
    model = Announcement
    new_display_title = _("Đăng bản tin")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.form.instance.pk:
            self.form_class.verbose_name = _("Sửa bản tin")

        return context

    def form_valid(self, form: AnnouncementForm) -> HttpResponse:
        if form.is_valid():
            creating = form.instance.pk is None
            message = (
                _("Đã đăng bản tin.") if creating else _("Đã cập nhật bản tin.")
            )

            anou, attachment_ids = form.save(commit=False)

            departments = form.cleaned_data["department"]
            job_positions = form.cleaned_data["job_position"]
            company = form.cleaned_data.get(
                "company_id", [self.request.user.employee_get.get_company()]
            )
            # The form no longer offers a per-employee selector, so the
            # audience is whatever the department and job position lists
            # say — and nothing at all means everybody in the company.
            targeted = bool(departments or job_positions)

            with transaction.atomic():
                anou.save()
                anou.attachments.add(*attachment_ids)
                anou.department.set(departments)
                anou.job_position.set(job_positions)
                anou.company_id.set(company)

                if targeted:
                    # An explicit audience is materialised into
                    # `employees`, because that set is what every
                    # visibility filter reads — the department and job
                    # position lists are not consulted there. `set` and
                    # not `add`: editing a post to narrow its audience has
                    # to actually narrow it.
                    # UNION, not intersection — unchanged from before this
                    # phase: choosing a department AND a job position reaches
                    # everyone in either, not only the people in both.
                    audience = Employee.objects.filter(
                        Q(employee_work_info__department_id__in=departments)
                        | Q(employee_work_info__job_position_id__in=job_positions)
                    ).distinct()
                    anou.employees.set(audience)
                else:
                    # A blank audience means "everybody in the selected
                    # company", and `employees` has to STAY blank to say
                    # so. This is the bug that emptied the mobile feed: the
                    # resolved list used to be written in here, which froze
                    # the audience at publication time. Nobody hired
                    # afterwards was in that snapshot and no filter ever
                    # recomputed it, so they could never see the post —
                    # while the web dashboard still showed it, because an
                    # admin holds `base.view_announcement` and skips the
                    # audience filter entirely.
                    #
                    # Clearing rather than leaving alone also means that
                    # re-saving an already-frozen post repairs it.
                    anou.employees.clear()
                    audience = Employee.objects.filter(is_active=True)
                    if company:
                        audience = audience.filter(
                            employee_work_info__company_id__in=company
                        )
                    audience = audience.distinct()

                    names = ", ".join(c.company for c in company if c is not None)
                    message = (
                        _(f"Đã đăng bản tin cho toàn bộ nhân viên tại {names}.")
                        if names
                        else _("Đã đăng bản tin cho toàn bộ nhân viên.")
                    )

                # Only a new post announces itself. Editing one — fixing a
                # typo in the title, extending the display window — must
                # not notify or push the whole company a second time.
                if creating and anou.send_notification:
                    notified_ids = self._record_announcement_notification(
                        anou, audience
                    )
                    if notified_ids:
                        # After commit, never inside it: a push cannot be
                        # rolled back, so announcing a post whose
                        # transaction then failed would be a notification
                        # about something that does not exist. Registered
                        # inside the atomic block so Django discards it if
                        # that happens. Primitives only, so the callback
                        # cannot read back a stale instance.
                        transaction.on_commit(
                            partial(
                                push_new_announcement,
                                anou.pk,
                                anou.title,
                                notified_ids,
                            )
                        )

            messages.success(self.request, message)
            return JoydigiRedirect(self.request)

        return super().form_valid(form)

    def _record_announcement_notification(self, announcement, audience):
        """Record the in-app notification once per recipient.

        Returns the ids of the users who actually got a row — which is not
        the same as the audience: `notify.send` drops anyone who turned
        notifications off. Those ids are what gets pushed, so a phone can
        never ring for something the in-app list does not have.

        Dedupe comes from the rows too. A double-submitted form, or a retry
        after a timeout, finds the recipients it already notified and sends
        them nothing.
        """
        recorded = announcement_notifications(announcement)
        already = set(recorded.values_list("recipient_id", flat=True))
        recipients = list(
            JoydigiUser.objects.filter(employee_get__in=audience)
            .exclude(pk__in=already)
            .distinct()
        )
        if not recipients:
            return ()

        notify.send(
            self.request.user.employee_get,
            recipient=recipients,
            verb=f"Có bản tin mới: {announcement.title}",
            redirect=reverse("bulletin"),
            icon="chatbox-ellipses",
            # Lets this announcement's own notifications be found again —
            # for the dedupe above, and for a future tap-to-open-the-post.
            action_object=announcement,
        )
        return tuple(set(recorded.values_list("recipient_id", flat=True)) - already)


@method_decorator(login_required, name="dispatch")
class AnnouncementDetailView(JoydigiDetailedView):

    model = Announcement
    template_name = "announcement/announcement_one.html"

    def get_context_data(self, **kwargs):
        import ast

        from joydigi.joydigi_middlewares import _thread_locals

        context = super().get_context_data(**kwargs)

        # Guard: if object was deleted or not found, close the modal gracefully
        if not self.instance:
            context["not_found"] = True
            context["extra_query"] = ""
            return context

        instance_ids = ast.literal_eval(self.request.GET.get("instance_ids", "[]"))
        url_info = resolve(self.request.path)
        url_name = url_info.url_name
        key = next(iter(url_info.kwargs), "pk")

        announcement_view_obj, _ = AnnouncementView.objects.get_or_create(
            user=self.request.user, announcement=self.instance
        )
        announcement_view_obj.viewed = True
        announcement_view_obj.save()

        context["announcement"] = self.instance

        if instance_ids:
            prev_id, next_id = closest_numbers(instance_ids, self.instance.pk)

            context.update(
                {
                    "instance_ids": str(instance_ids),
                    "ids_key": self.ids_key,
                    "next_url": reverse(url_name, kwargs={key: next_id}),
                    "previous_url": reverse(url_name, kwargs={key: prev_id}),
                }
            )

            get_params = self.request.GET.copy()
            get_params.pop(self.ids_key, None)
            context["extra_query"] = get_params.urlencode()
        else:
            context["extra_query"] = ""

        return context
