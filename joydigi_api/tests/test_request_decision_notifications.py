"""Phase REQUEST-STATUS-AND-APPROVAL-NOTIFICATIONS — the employee is told.

`base/request_decisions.py` hangs off every request model's own `save()`
rather than off the fifty-odd screens that can approve something, so these
tests drive the thing a screen actually does — flip the row and save it —
and assert on what the employee receives. The two state derivations the
module has to get right are both exercised: `leave.LeaveRequest` carries a
real four-value `status` CharField, while the other six carry only an
`approved`/`canceled` boolean pair.

What is held here, beyond "an approval notifies":

* The wire contract is asserted literally, not read back out of the module
  under test. The Flutter client parses `data["type"]` into a destination
  (`appcheckin/lib/features/push/domain/push_notification_type.dart`), so a
  renamed string is a silently dead notification on every phone — a test
  that mirrors the constant would not notice.
* Nothing is announced twice. A double-click on an approve button saves the
  same row again, and `approved -> approved` is not a transition.
* Nothing is announced that the employee did themselves. The six boolean
  models write the *same* `canceled` bit for an approver's "Từ chối" and for
  the owner's own withdrawal (`RemoteWorkRequestCancelAPIView`,
  `joydigi_api/api_views/attendance/views.py:2144`, sets `canceled = True` at
  :2168), so `modified_by` is the only thing that can tell them apart.
* An approval is the manager's work and a push is not, so a dead transport
  must never cost the approval.

Firebase is mocked throughout. Nothing here contacts Google, and no token
value is asserted on.
"""

from datetime import date
from unittest import mock

from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from notifications.models import Notification

from attendance.models import (
    AttendanceExplanationRequest,
    AttendanceLateEarlyRequest,
    OvertimeRequest,
    RemoteWorkRequest,
)
from base import request_decisions
from base.models import (
    CompanyGroupAssignment,
    EmployeeShift,
    ShiftRequest,
    WorkType,
    WorkTypeRequest,
)
from base.roles import LEADER_ROLE
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api import push as push_module
from joydigi_api.models import NotificationPreference, PushDeviceToken

# The same patch target the announcement push tests use
# (`joydigi_api/tests/test_announcement_push.py:35`) and the same one the
# check-in reminders patch at their call site
# (`attendance/tests/test_checkin_reminders.py:110`). `_push_decision`
# imports `send_to_user` from the module at call time, so patching the
# module attribute is what intercepts it.
from joydigi_api.tests.test_announcement_push import PUSH_TARGET
from leave.models import LeaveRequest, LeaveType

#: What a successful `send_to_user` answers. Copied per use so one test's
#: mock cannot hand a mutated dict to the next.
PUSH_RESULT = {
    "sent": 1,
    "failed": 0,
    "deactivated": 0,
    "skipped": False,
    "status": None,
}

# Fixed, far-future dates rather than `date.today() + timedelta(...)`: the
# body of the push contains the formatted date, so the expected string has
# to be writable in full. A relative date would make these assertions
# re-derive the very formatting they are meant to pin.
ONE_DAY = date(2030, 5, 7)
RANGE_START = date(2030, 5, 7)
RANGE_END = date(2030, 5, 9)

ONE_DAY_TEXT = "ngày 07/05/2030"
RANGE_TEXT = "từ ngày 07/05/2030 đến ngày 09/05/2030"

APPROVED_TITLE = "Đơn của bạn đã được duyệt"
REJECTED_TITLE = "Đơn của bạn đã bị từ chối"


def decision_rows(instance):
    """The in-app rows this module wrote about `instance`, and only those.

    Found through the generic `action_object` FK the way the module's own
    dedupe does, then narrowed in Python to rows carrying the marker key.
    Both halves matter: several other features already notify about these
    same request rows (`attendance/views/requests.py:569` and its
    neighbours), so a bare count of the employee's notifications would be
    measuring those too, and a `data__key=value` JSON filter does not
    behave identically on SQLite and PostgreSQL — the reason
    `request_decisions.already_sent` compares in Python as well.
    """
    rows = Notification.objects.filter(
        action_object_content_type=ContentType.objects.get_for_model(
            instance, for_concrete_model=True
        ),
        action_object_object_id=str(instance.pk),
    )
    return [row for row in rows if (row.data or {}).get(request_decisions.MARKER_KEY)]


class RequestDecisionTestCase(TestCase):
    """Shared harness: the employee who files requests, plus two people who
    must never hear about them — a colleague in the same company and an
    employee of another company."""

    def setUp(self):
        self.company = make_company("Decision Co")

        self.owner_user = make_user("decision_owner")
        self.owner = make_employee(
            company=self.company,
            email="decision-owner@test.joydigi",
            user=self.owner_user,
        )

        self.bystander_user = make_user("decision_bystander")
        self.bystander = make_employee(
            company=self.company,
            email="decision-bystander@test.joydigi",
            user=self.bystander_user,
        )

        self.other_company = make_company("Decision Other Co")
        self.outsider_user = make_user("decision_outsider")
        self.outsider = make_employee(
            company=self.other_company,
            email="decision-outsider@test.joydigi",
            user=self.outsider_user,
        )

        self.leave_type = LeaveType.objects.create(
            company_id=self.company,
            name="Nghỉ phép năm",
            payment="paid",
            total_days=12,
        )

    # ------------------------------------------------------------ factories

    def make_leave(self, employee=None, **overrides):
        defaults = {
            "employee_id": employee or self.owner,
            "leave_type_id": self.leave_type,
            "start_date": RANGE_START,
            "end_date": RANGE_END,
            # Both breakdowns are deliberately "full_day". With a one-day
            # request and two different breakdowns, `leave/signals.py:31`
            # re-saves the row from inside the post_save it is handling, and
            # that nested save is a different scenario from the one under
            # test here.
            "start_date_breakdown": "full_day",
            "end_date_breakdown": "full_day",
            "description": "Nghỉ phép theo kế hoạch.",
            "status": "requested",
        }
        defaults.update(overrides)
        return LeaveRequest.objects.create(**defaults)

    def make_remote(self, employee=None, **overrides):
        defaults = {
            "employee_id": employee or self.owner,
            "start_date": ONE_DAY,
            "end_date": ONE_DAY,
            "description": "Chăm sóc con nhỏ tại nhà.",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return RemoteWorkRequest.objects.create(**defaults)

    def make_overtime(self, employee=None, **overrides):
        defaults = {
            "employee_id": employee or self.owner,
            "request_date": ONE_DAY,
            "start_time": "18:00",
            "end_time": "21:00",
            "description": "Hoàn thành bản phát hành.",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return OvertimeRequest.objects.create(**defaults)

    def make_late_early(self, employee=None, **overrides):
        defaults = {
            "employee_id": employee or self.owner,
            "request_type": "late_arrival",
            "request_date": ONE_DAY,
            "requested_time": "09:30",
            "description": "Đưa con đi khám bệnh.",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return AttendanceLateEarlyRequest.objects.create(**defaults)

    def make_explanation(self, employee=None, **overrides):
        defaults = {
            "employee_id": employee or self.owner,
            "request_type": "missing_check_in",
            "request_date": ONE_DAY,
            "description": "Tôi quên chấm công khi đến văn phòng.",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return AttendanceExplanationRequest.objects.create(**defaults)

    # -------------------------------------------------------------- harness

    def patched_push(self, **kwargs):
        kwargs.setdefault("return_value", dict(PUSH_RESULT))
        return mock.patch(PUSH_TARGET, **kwargs)

    @staticmethod
    def set_state(instance, state):
        """Write `state` onto the row exactly as an approval screen does.

        `base/checkin_portal.py:687` sets `approved = True, canceled = False`
        and `:709` the mirror image; the leave screens write the `status`
        string. Nothing else about the row is touched, because an approval
        in this codebase is only those fields plus `save()`.
        """
        if isinstance(instance, LeaveRequest):
            instance.status = state
        else:
            instance.approved = state == request_decisions.APPROVED
            instance.canceled = state == request_decisions.REJECTED

    def decide(self, instance, state, actor=None):
        """Apply a decision and let the deferred push actually run.

        The push is registered with `transaction.on_commit`, and a `TestCase`
        never commits, so without `captureOnCommitCallbacks(execute=True)`
        every push assertion in this file would pass vacuously. Returns the
        patched transport so the caller can read the payload.
        """
        self.set_state(instance, state)
        if actor is not None:
            # What `JoydigiModel.save()` stamps from the thread-local request
            # (`joydigi/models.py:164`). Set by hand because a direct save in
            # a test has no request behind it.
            instance.modified_by = actor
        with self.patched_push() as send:
            with self.captureOnCommitCallbacks(execute=True):
                instance.save()
        return send

    def reset_push_module(self):
        push_module._app = None
        push_module._app_attempted = False


class ApprovalAndRejectionAreAnnouncedTests(RequestDecisionTestCase):
    def test_approving_a_pending_request_writes_one_row_and_one_push(self):
        """One approval, one in-app row, one push — for five request types.

        `leave` derives its state from a `status` CharField and the other
        four from the `approved`/`canceled` boolean pair, so both code paths
        in the module are covered by this one loop rather than by five
        near-identical tests.
        """
        cases = [
            ("leave", self.make_leave, f"Đơn nghỉ phép {RANGE_TEXT}"),
            ("remote", self.make_remote, f"Đơn làm việc từ xa {ONE_DAY_TEXT}"),
            ("overtime", self.make_overtime, f"Đơn làm thêm giờ {ONE_DAY_TEXT}"),
            ("late_early", self.make_late_early, f"Đơn đi muộn/về sớm {ONE_DAY_TEXT}"),
            ("explanation", self.make_explanation, f"Đơn giải trình {ONE_DAY_TEXT}"),
        ]

        for slug, build, subject in cases:
            with self.subTest(request_type=slug):
                instance = build()
                self.assertEqual(decision_rows(instance), [])

                send = self.decide(instance, request_decisions.APPROVED)

                rows = decision_rows(instance)
                self.assertEqual(len(rows), 1, msg="exactly one row, no duplicate")
                self.assertEqual(rows[0].recipient_id, self.owner_user.pk)
                self.assertEqual(rows[0].verb, f"{subject} đã được duyệt.")
                self.assertEqual(
                    rows[0].data[request_decisions.MARKER_KEY],
                    f"{slug}:{instance.pk}:approved",
                )

                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[0].pk, self.owner_user.pk)
                self.assertEqual(send.call_args.args[1], APPROVED_TITLE)
                self.assertEqual(
                    send.call_args.kwargs["data"]["type"], "request_approved"
                )
                self.assertEqual(
                    send.call_args.kwargs["data"]["request_type"], slug
                )

    def test_rejecting_a_pending_request_announces_the_rejection(self):
        """The rejected wording and type, for both state derivations.

        `leave` can say "rejected" outright; `remote` can only be told
        through its `canceled` bit, which is the overloaded one — so the
        actor is a colleague here, not the owner, which is what makes the
        bit mean "a manager refused this".
        """
        cases = [
            ("leave", self.make_leave, f"Đơn nghỉ phép {RANGE_TEXT}"),
            ("remote", self.make_remote, f"Đơn làm việc từ xa {ONE_DAY_TEXT}"),
        ]

        for slug, build, subject in cases:
            with self.subTest(request_type=slug):
                instance = build()

                send = self.decide(
                    instance,
                    request_decisions.REJECTED,
                    actor=self.bystander_user,
                )

                rows = decision_rows(instance)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].recipient_id, self.owner_user.pk)
                self.assertEqual(rows[0].verb, f"{subject} đã bị từ chối.")

                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[1], REJECTED_TITLE)
                data = send.call_args.kwargs["data"]
                self.assertEqual(data["type"], "request_rejected")
                self.assertEqual(data["status"], "rejected")
                self.assertEqual(data["request_type"], slug)


class PushPayloadWireContractTests(RequestDecisionTestCase):
    def test_push_payload_matches_the_wire_contract(self):
        """Title, body and every data key, asserted as literals.

        The three cases together cover both date shapes the module can
        produce: a `_range` that collapses to one day, a real `_range`, and
        a `_one_day` field. A wrong shape here reads to the employee as a
        notification about the wrong request.
        """
        single = self.make_remote()
        send = self.decide(single, request_decisions.APPROVED)

        user, title, body = send.call_args.args
        data = send.call_args.kwargs["data"]
        self.assertEqual(user.pk, self.owner_user.pk)
        self.assertEqual(title, "Đơn của bạn đã được duyệt")
        self.assertEqual(body, "Đơn làm việc từ xa ngày 07/05/2030 đã được duyệt.")
        self.assertEqual(
            data,
            {
                "type": "request_approved",
                "request_type": "remote",
                "request_id": str(single.pk),
                "status": "approved",
            },
            msg="ids and a type only — no request content goes over the wire",
        )
        self.assertIsInstance(
            data["request_id"],
            str,
            msg="the client reads it as a string; an int would not parse",
        )

        ranged = self.make_remote(start_date=RANGE_START, end_date=RANGE_END)
        send = self.decide(ranged, request_decisions.APPROVED)
        self.assertEqual(
            send.call_args.args[2],
            "Đơn làm việc từ xa từ ngày 07/05/2030 đến ngày 09/05/2030 "
            "đã được duyệt.",
        )

        overtime = self.make_overtime()
        send = self.decide(
            overtime, request_decisions.REJECTED, actor=self.bystander_user
        )
        _user, title, body = send.call_args.args
        self.assertEqual(title, "Đơn của bạn đã bị từ chối")
        self.assertEqual(body, "Đơn làm thêm giờ ngày 07/05/2030 đã bị từ chối.")
        self.assertEqual(
            send.call_args.kwargs["data"],
            {
                "type": "request_rejected",
                "request_type": "overtime",
                "request_id": str(overtime.pk),
                "status": "rejected",
            },
        )

        # A push is readable from a locked screen, so what the employee wrote
        # must not be in it. Checked as a substring of the real strings, not as
        # membership of `[title, body]`: that list form only asks whether the
        # description IS the whole title or the whole body, which the literal
        # assertions above have already settled, so it could never fail.
        for text in (title, body):
            self.assertNotIn(
                overtime.description,
                text,
                msg="what the employee wrote must not travel in the push",
            )


class NothingIsAnnouncedTwiceTests(RequestDecisionTestCase):
    def test_re_saving_an_approved_row_and_a_repeated_call_send_nothing(self):
        """The double-click, and then the marker behind it.

        Two independent guards, so both are held. Saving an already-approved
        row is not a transition at all, which is what covers an impatient
        second POST. The marker in `Notification.data` is the second line of
        defence: it makes one decision unrepeatable even for a caller that
        reaches the service directly, as a future write that bypasses
        `save()` is meant to.
        """
        instance = self.make_remote()
        first = self.decide(instance, request_decisions.APPROVED)
        self.assertEqual(first.call_count, 1)
        self.assertEqual(len(decision_rows(instance)), 1)

        second = self.decide(instance, request_decisions.APPROVED)

        self.assertEqual(second.call_count, 0, msg="approved -> approved is no news")
        self.assertEqual(len(decision_rows(instance)), 1)

        with self.patched_push() as direct:
            with self.captureOnCommitCallbacks(execute=True):
                announced = request_decisions.notify_request_decision(
                    instance, request_decisions.APPROVED
                )

        self.assertFalse(
            announced, msg="the marker already written for this decision was found"
        )
        self.assertEqual(direct.call_count, 0)
        self.assertEqual(len(decision_rows(instance)), 1)


class CreationIsNotADecisionTests(RequestDecisionTestCase):
    def test_creating_a_request_sends_nothing_even_when_born_approved(self):
        """Filing a request is not news, and neither is one born approved.

        A leave type with `require_approval == "no"` creates an already
        approved request; the employee learns that from the response to
        their own submission, so announcing it would be the app telling
        somebody what they just did.
        """
        with self.patched_push() as send:
            with self.captureOnCommitCallbacks(execute=True):
                pending_remote = self.make_remote()
                born_approved_remote = self.make_remote(
                    approved=True,
                    start_date=RANGE_START,
                    end_date=RANGE_END,
                )
                born_approved_leave = self.make_leave(status="approved")

        send.assert_not_called()
        for instance in (pending_remote, born_approved_remote, born_approved_leave):
            self.assertEqual(
                decision_rows(instance),
                [],
                msg=f"nothing announced for {type(instance).__name__}",
            )


class OwnDecisionIsNotAnnouncedTests(RequestDecisionTestCase):
    def test_the_owner_withdrawing_their_own_request_is_not_announced(self):
        """Cancelling your own request must not push you a rejection.

        `RemoteWorkRequest` and the five other boolean models have one
        `canceled` bit that both an approver's "Từ chối" and the owner's own
        withdrawal write, so the state derivation alone cannot tell them
        apart — `modified_by` is the only difference, and this is the test
        that it is consulted. The contrast half matters as much as the first:
        without it, a `_decided_by_owner` that answered True for everybody
        would silence every notification in production and still pass.
        """
        mine = self.make_remote()
        send = self.decide(
            mine, request_decisions.REJECTED, actor=self.owner_user
        )

        self.assertEqual(
            decision_rows(mine), [], msg="I withdrew it; I do not need telling"
        )
        send.assert_not_called()
        mine.refresh_from_db()
        self.assertTrue(mine.canceled, msg="the withdrawal itself still happened")

        theirs = self.make_remote(start_date=RANGE_START, end_date=RANGE_END)
        send = self.decide(
            theirs, request_decisions.REJECTED, actor=self.bystander_user
        )

        self.assertEqual(
            len(decision_rows(theirs)),
            1,
            msg="the same bit written by somebody else is a real rejection",
        )
        self.assertEqual(send.call_count, 1)


class OnlyTheOwnerIsNotifiedTests(RequestDecisionTestCase):
    def test_only_the_owner_is_notified(self):
        """One decision reaches exactly one account.

        A request carries somebody's reason for being away, so a colleague
        receiving the decision would be a disclosure, and an employee of
        another company receiving it would be a tenancy leak.
        """
        instance = self.make_remote()

        send = self.decide(instance, request_decisions.APPROVED)

        rows = decision_rows(instance)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].recipient_id, self.owner_user.pk)
        self.assertEqual(
            {call.args[0].pk for call in send.call_args_list},
            {self.owner_user.pk},
        )
        self.assertEqual(send.call_count, 1)

        self.assertEqual(
            Notification.objects.filter(recipient=self.bystander_user).count(),
            0,
            msg="a colleague in the same company hears nothing",
        )
        self.assertEqual(
            Notification.objects.filter(recipient=self.outsider_user).count(),
            0,
            msg="an employee of another company hears nothing",
        )


class PushFailureIsolationTests(RequestDecisionTestCase):
    def test_an_approval_survives_a_broken_push_transport(self):
        """The manager's click is the work; the push is not.

        Two ways the transport can fail. A raising `send_to_user` is the
        network or the SDK going wrong, and is swallowed by
        `_push_decision`. No Firebase credential at all is the ordinary
        state of this test environment and of any developer machine, and
        takes the real `send_to_user` path — which is why a device token is
        registered for it, so the sender gets as far as looking for an app.
        """
        raising = self.make_remote()
        self.set_state(raising, request_decisions.APPROVED)
        with self.patched_push(side_effect=RuntimeError("firebase down")):
            with self.captureOnCommitCallbacks(execute=True):
                raising.save()

        raising.refresh_from_db()
        self.assertTrue(raising.approved, msg="the approval stands")
        self.assertEqual(len(decision_rows(raising)), 1, msg="the in-app row stands")

        PushDeviceToken.objects.create(
            user=self.owner_user,
            token="device-for-the-owner",
            platform=PushDeviceToken.ANDROID,
        )
        self.reset_push_module()
        self.addCleanup(self.reset_push_module)

        unconfigured = self.make_remote(
            start_date=RANGE_START, end_date=RANGE_END
        )
        self.set_state(unconfigured, request_decisions.APPROVED)
        # No mock: the real sender runs and reports
        # PUSH_SKIPPED_FIREBASE_NOT_CONFIGURED, as production would on a
        # deployment whose credential went missing.
        #
        # The credential setting is pinned empty rather than left to whatever
        # the developer's environment holds. Without that, a machine that does
        # have a real credential file would try to reach Firebase from the test
        # run — slow at best, and the assertion below would then be passing for
        # a different reason than the one it is written for.
        with self.settings(FIREBASE_CREDENTIALS_FILE=""):
            with self.captureOnCommitCallbacks(execute=True):
                unconfigured.save()

        unconfigured.refresh_from_db()
        self.assertTrue(unconfigured.approved)
        self.assertEqual(len(decision_rows(unconfigured)), 1)


class NotificationPreferenceTests(RequestDecisionTestCase):
    def test_a_recipient_with_notifications_off_gets_neither_row_nor_push(self):
        """The opt-out has to silence the phone as well as the list.

        `joydigi_api.push` never consults `NotificationPreference`, so the
        only thing that honours it is the service declining to register the
        push when `notify.send` wrote no row. A phone ringing for a
        notification the in-app list does not contain is the bug this pins.
        """
        NotificationPreference.objects.create(
            user=self.owner_user, all_notifications_enabled=False
        )
        instance = self.make_remote()

        send = self.decide(instance, request_decisions.APPROVED)

        self.assertEqual(decision_rows(instance), [])
        send.assert_not_called()
        instance.refresh_from_db()
        self.assertTrue(
            instance.approved, msg="an opt-out does not undo an approval"
        )


class IosSoundTests(RequestDecisionTestCase):
    def test_a_decision_push_asks_ios_for_a_sound_and_leaves_android_alone(self):
        """A decision push is audible on an iPhone, and Android is untouched.

        This goes through the real `joydigi_api.push.send_to_user` rather
        than a mock of it, because the claim is about the APNs payload that
        function builds. The `firebase_admin` stand-in and the recording
        `Message` are the ones `PushSenderTests` already owns
        (`joydigi_api/tests/test_push_device_token.py:165` and `:178`) — a
        third fake would be a third thing to keep in step with the SDK.
        They are imported inside the test and called with the class itself
        as `self`: `fake_firebase` needs nothing from an instance except
        `_recording_message`, which is a staticmethod, and importing that
        `TestCase` at module level would make this module re-run all of its
        tests as well.
        """
        from joydigi_api.tests.test_push_device_token import PushSenderTests

        PushDeviceToken.objects.create(
            user=self.owner_user,
            token="iphone-of-the-owner",
            platform=PushDeviceToken.IOS,
        )
        self.reset_push_module()
        self.addCleanup(self.reset_push_module)

        messaging, patched_sdk = PushSenderTests.fake_firebase(PushSenderTests)

        instance = self.make_remote()
        self.set_state(instance, request_decisions.APPROVED)
        with mock.patch.object(push_module, "_load_app", return_value=object()):
            with patched_sdk:
                with self.captureOnCommitCallbacks(execute=True):
                    instance.save()

        self.assertEqual(messaging.send.call_count, 1)
        message = messaging.send.call_args.args[0]
        self.assertEqual(
            message.apns.payload.aps.sound,
            "default",
            msg="iOS plays nothing without aps.sound, and FCM adds none",
        )
        self.assertNotIn(
            "android",
            message.sent_kwargs,
            msg=(
                "the app's own high-importance channel decides Android sound; "
                "an AndroidConfig here would be a second place deciding it"
            ),
        )
        self.assertIn("apns", message.sent_kwargs)
        self.assertEqual(message.data["type"], "request_approved")
        self.assertEqual(message.data["request_id"], str(instance.pk))


class RealApprovalEntryPointTests(TestCase):
    """Phase REQUEST-STATUS-AND-APPROVAL-NOTIFICATIONS — through a real screen.

    Every other test in this file flips the row itself, which is the right
    unit for the module but proves nothing about the premise the whole design
    rests on: that the approval screens really do go through the model's
    `save()`. So this one drives `base/checkin_portal.py:672`
    `remote_request_approve` over HTTP, as a leader with the permission the
    view requires — the setUp shape is
    `base/tests/test_checkin_approval_hub.py:1794`, where the same leader,
    group assignment and reporting line are already worked out.

    It also covers the one thing a direct save cannot: a real request means
    `JoydigiModel.save()` stamps `modified_by` with the *approver*, so this
    is where `_decided_by_owner` has to answer False on live data rather
    than merely because a test left the field blank.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Decision Hub Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("decision_hub_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="decision-hub-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )

        cls.worker_user = make_user("decision_hub_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="decision-hub-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

    def test_approving_through_the_approval_hub_notifies_the_employee(self):
        pending = RemoteWorkRequest.objects.create(
            employee_id=self.worker,
            start_date=ONE_DAY,
            end_date=ONE_DAY,
            description="Làm việc tại nhà một ngày.",
            approved=False,
            canceled=False,
        )

        self.client.force_login(self.leader_user)
        with mock.patch(PUSH_TARGET, return_value=dict(PUSH_RESULT)) as send:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("remote-request-approve", kwargs={"id": pending.id}),
                    HTTP_REFERER=reverse("approval-hub"),
                )

        self.assertEqual(response.status_code, 302)
        pending.refresh_from_db()
        self.assertTrue(pending.approved)
        self.assertEqual(
            pending.modified_by_id,
            self.leader_user.pk,
            msg="the approver is stamped, which is what the owner check reads",
        )

        rows = decision_rows(pending)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].recipient_id, self.worker_user.pk)
        self.assertEqual(
            rows[0].verb, "Đơn làm việc từ xa ngày 07/05/2030 đã được duyệt."
        )

        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0].pk, self.worker_user.pk)
        self.assertEqual(send.call_args.args[1], APPROVED_TITLE)
        self.assertEqual(
            send.call_args.kwargs["data"],
            {
                "type": "request_approved",
                "request_type": "remote",
                "request_id": str(pending.pk),
                "status": "approved",
            },
        )
        self.assertEqual(
            Notification.objects.filter(recipient=self.leader_user).count(),
            0,
            msg="the approver is not told about their own click",
        )


class InAppPayloadTests(RequestDecisionTestCase):
    def test_the_in_app_row_carries_the_same_contract_as_the_push(self):
        """The notification list is the other half of the same contract.

        A phone that was switched off when the decision was made never sees
        the push; the in-app list is where it finds out. So the row has to
        carry the same identifying keys the push does, and nothing was
        asserting that — the existing tests check the row's marker and verb
        only, which means deleting the data payload from the `notify.send`
        call would leave them all green while the list lost the ability to
        say which request a line is about.
        """
        remote = self.make_remote()
        self.decide(remote, request_decisions.APPROVED)

        row = decision_rows(remote)[0]
        for key, value in (
            ("type", "request_approved"),
            ("request_type", "remote"),
            ("request_id", str(remote.pk)),
            ("status", "approved"),
        ):
            self.assertEqual(row.data.get(key), value, msg=key)
        self.assertEqual(
            row.data.get(request_decisions.MARKER_KEY),
            f"remote:{remote.pk}:approved",
        )


class EveryRegisteredKindTests(RequestDecisionTestCase):
    """The two kinds nothing else reaches.

    `ShiftRequest` and `WorkTypeRequest` need a shift and a work type that the
    other five request types do not, so they were left out of the first pass
    and their registry entries — slug, Vietnamese noun, and the
    `requested_date`/`requested_till` pair — were asserted by nothing. A typo
    in either noun would have shipped.
    """

    def test_a_shift_change_decision_is_announced(self):
        shift = EmployeeShift.objects.create(employee_shift="Ca chiều")
        request = ShiftRequest.objects.create(
            employee_id=self.owner,
            shift_id=shift,
            requested_date=RANGE_START,
            requested_till=RANGE_END,
            description="Đổi sang ca chiều.",
            approved=False,
            canceled=False,
        )

        send = self.decide(request, request_decisions.APPROVED)

        self.assertEqual(len(decision_rows(request)), 1)
        self.assertEqual(
            send.call_args.args[2],
            f"Đơn đổi ca {RANGE_TEXT} đã được duyệt.",
        )
        self.assertEqual(send.call_args.kwargs["data"]["request_type"], "shift")

    def test_a_work_type_decision_is_announced(self):
        work_type = WorkType.objects.create(work_type="Tại nhà")
        request = WorkTypeRequest.objects.create(
            employee_id=self.owner,
            work_type_id=work_type,
            requested_date=ONE_DAY,
            requested_till=ONE_DAY,
            description="Xin làm tại nhà.",
            approved=False,
            canceled=False,
        )

        send = self.decide(request, request_decisions.REJECTED, actor=self.bystander_user)

        self.assertEqual(len(decision_rows(request)), 1)
        self.assertEqual(
            send.call_args.args[2],
            f"Đơn đổi loại hình làm việc {ONE_DAY_TEXT} đã bị từ chối.",
        )
        self.assertEqual(
            send.call_args.kwargs["data"],
            {
                "type": "request_rejected",
                "request_type": "work_type",
                "request_id": str(request.pk),
                "status": "rejected",
            },
        )

    def test_every_registered_kind_can_describe_itself(self):
        """No registry entry may be half-filled.

        Cheap, and it is what catches a seventh kind being added later with a
        missing noun or a date field that the model does not have.
        """
        for model, kind in request_decisions.registry().items():
            self.assertTrue(kind.slug, msg=model.__name__)
            self.assertTrue(kind.noun.startswith("Đơn"), msg=model.__name__)
            self.assertTrue(kind.state_fields, msg=model.__name__)
            for field in kind.state_fields:
                # A field name that does not exist would make `_state_in_db`
                # raise on every save of that model.
                model._meta.get_field(field)


class NestedSaveTests(RequestDecisionTestCase):
    """A receiver that re-saves the row must not erase the transition.

    `leave/signals.py:28-31` does exactly that for a one-day leave request
    whose two breakdowns disagree: it rewrites `end_date_breakdown` and calls
    `save()` again from inside the `post_save` it is handling. Today this
    module's receiver happens to run first, so the decision is announced
    before that nested save — but the order is only the order of
    `INSTALLED_APPS`, and a notification should not depend on it.
    """

    def test_a_nested_save_cannot_erase_the_remembered_state(self):
        remote = self.make_remote()
        request_decisions._remember_previous_state(RemoteWorkRequest, remote)
        self.assertEqual(
            getattr(remote, request_decisions._PREVIOUS),
            request_decisions.PENDING,
        )

        # The outer save has now written the approval, so the database says
        # "approved" — this is precisely the state a nested `pre_save` would
        # read back.
        RemoteWorkRequest.objects.filter(pk=remote.pk).update(approved=True)
        request_decisions._remember_previous_state(RemoteWorkRequest, remote)

        self.assertEqual(
            getattr(remote, request_decisions._PREVIOUS),
            request_decisions.PENDING,
            msg="the nested save must keep the state the outer save started from",
        )

    def test_a_one_day_leave_request_with_mismatched_breakdowns_is_announced_once(self):
        # The shape that triggers the nested save in `leave/signals.py`.
        leave = self.make_leave(
            start_date=ONE_DAY,
            end_date=ONE_DAY,
            start_date_breakdown="first_half",
            end_date_breakdown="second_half",
        )

        send = self.decide(leave, request_decisions.APPROVED)

        self.assertEqual(
            len(decision_rows(leave)),
            1,
            msg="exactly one row, whichever receiver ran first",
        )
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], APPROVED_TITLE)

