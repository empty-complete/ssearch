"""Developer evidence state; records facts and never executes validation/builds."""
from .config import SHA, require
from .contracts import BuildEvidence, CheckEvidence, ContributionError, ReviewEvidence
from .validation import DIGEST, ReceiptStore, ValidationRecipe, snapshot


class ContributionRun:
    def __init__(self, *, workspace, implementer_id: str, recipe: ValidationRecipe,
                 receipts: ReceiptStore):
        require(isinstance(implementer_id, str) and 0 < len(implementer_id.strip()) <= 128,
                "invalid_implementer")
        require(isinstance(recipe, ValidationRecipe) and isinstance(receipts, ReceiptStore)
                and recipe.repository_id == receipts.repository_id, "invalid_recipe")
        self.workspace = workspace
        self.implementer_id = implementer_id
        self.recipe = recipe
        self.receipts = receipts
        self._regression = None
        self._checks = {}
        self._review = None
        self._target = None
        self._build = None
        snapshot(workspace)  # require an actual standalone repository, never a prose fingerprint

    def _invalidate_later(self):
        self._review = self._target = self._build = None

    def _verified_regression(self, red, green):
        red_pin = self.receipts.verify(red, self.recipe)
        green_pin = self.receipts.verify(green, self.recipe)
        require(red_pin is not None and green_pin is not None, "invalid_regression")
        spec = self.recipe.spec(red.command_id)
        if spec.report_kind == "source":
            # Aggregate pytest exit codes do not distinguish assertion failures
            # from setup/teardown errors, or a genuine GREEN from a skipped test.
            # The host authenticates phase reports; Source JSON has no phases.
            for pin, outcome in ((red_pin, "failed"), (green_pin, "passed")):
                require(pin.test_phases == ((spec.targets[0], "setup", "passed"),
                    (spec.targets[0], "call", outcome), (spec.targets[0], "teardown", "passed")),
                    "regression_phase_attestation_required")
        return red_pin, green_pin

    def record_regression(self, red: CheckEvidence, green: CheckEvidence) -> None:
        require(isinstance(red, CheckEvidence) and isinstance(green, CheckEvidence)
                and red.command_id == green.command_id, "regression_id_mismatch")
        spec = self.recipe.spec(red.command_id)
        require(spec.scope == "regression" and spec.report_kind in {"source", "synthetic"}
                and len(spec.targets) == 1 and red.status == "failed" and red.exit_code == 1
                and green.status == "passed" and green.exit_code == 0, "invalid_regression")
        red_pin, green_pin = self._verified_regression(red, green)
        require(red_pin is not None and green_pin is not None
                and red.report_sha256 != green.report_sha256, "invalid_regression")
        current = snapshot(self.workspace)
        require(green_pin.source_sha == current.source_sha and green_pin.tree_sha256 == current.tree_sha256,
                "regression_source_mismatch")
        self._regression = (red, green)
        self._checks.clear()
        self._invalidate_later()

    def record_checks(self, scope: str, evidence: CheckEvidence) -> None:
        require(isinstance(evidence, CheckEvidence), "invalid_evidence")
        spec = self.recipe.spec(evidence.command_id)
        require(scope == spec.scope and scope != "regression", "check_scope_mismatch")
        pin = self.receipts.verify(evidence, self.recipe)
        current = snapshot(self.workspace)
        require(evidence.source_sha == current.source_sha, "check_source_mismatch")
        if pin is not None:
            require(pin.tree_sha256 == current.tree_sha256, "check_source_mismatch")
        old = self._checks.get(evidence.command_id)
        self._checks[evidence.command_id] = evidence
        # Adding an honest optional limitation does not alter reviewed source or
        # checks. Replacing an actual result requires review of the new evidence.
        if evidence.status != "not_run" or old is not None:
            self._invalidate_later()

    def record_review(self, review: ReviewEvidence) -> None:
        require(isinstance(review, ReviewEvidence) and isinstance(review.reviewer_id, str)
                and 0 < len(review.reviewer_id.strip()) <= 128
                and review.reviewer_id.strip().casefold() != self.implementer_id.strip().casefold()
                and not any(ord(char) < 32 for char in review.reviewer_id)
                and type(review.blocking_findings) is int and review.blocking_findings == 0
                and isinstance(review.reviewed_tree_sha256, str)
                and DIGEST.fullmatch(review.reviewed_tree_sha256) is not None, "invalid_review")
        require(self._regression is not None, "missing_regression")
        current = snapshot(self.workspace)
        require(review.reviewed_tree_sha256 == current.tree_sha256, "review_tree_mismatch")
        _, green = self._verified_regression(*self._regression)
        require(green is not None and green.tree_sha256 == current.tree_sha256, "review_tree_mismatch")
        self._review = review
        self._target = self._build = None

    def record_commit(self, target_sha: str) -> None:
        require(isinstance(target_sha, str) and SHA.fullmatch(target_sha) is not None,
                "invalid_target")
        require(self._review is not None, "missing_review")
        current = snapshot(self.workspace)
        require(current.clean and current.source_sha == target_sha
                and current.tree_sha256 == self._review.reviewed_tree_sha256, "commit_source_mismatch")
        _, green = self._verified_regression(*self._regression)
        require(green is not None and green.tree_sha256 == current.tree_sha256, "commit_source_mismatch")
        self._target = target_sha
        self._build = None

    def record_build(self, evidence: BuildEvidence) -> None:
        require(isinstance(evidence, BuildEvidence), "invalid_build")
        require(self._target is not None and self._review is not None, "commit_required_before_build")
        current = snapshot(self.workspace)
        require(current.clean and current.source_sha == self._target == evidence.source_sha
                and current.tree_sha256 == self._review.reviewed_tree_sha256, "build_source_mismatch")
        self._verify_build(evidence, current)
        self._build = evidence

    def _verify_build(self, evidence, current):
        pin = self.receipts.verify_build(evidence, self.recipe)
        require(pin.tree_sha256 == current.tree_sha256 and pin.source_sha == self._target,
                "build_source_mismatch")
        require(isinstance(evidence.frozen_checks, tuple)
                and all(isinstance(item, CheckEvidence) for item in evidence.frozen_checks), "invalid_build")
        commands = [item.command_id for item in evidence.frozen_checks]
        require(len(set(commands)) == len(commands) and set(self.recipe.required_frozen) <= set(commands),
                "missing_frozen_checks")
        for check in evidence.frozen_checks:
            require(self.recipe.spec(check.command_id).report_kind == "frozen"
                    and check.status == "passed" and check.source_sha == self._target, "invalid_frozen_receipt")
            frozen_pin = self.receipts.verify(check, self.recipe)
            require(frozen_pin is not None and frozen_pin.tree_sha256 == current.tree_sha256,
                    "frozen_source_mismatch")

    def ready(self) -> bool:
        """Reverify current source and receipts; pending required gates stay false.

        For a fixture recipe this is ONLY fixture readiness. It cannot authorize
        production proposals, package acceptance, publication or native claims.
        """
        try:
            if self._regression is None or self._review is None or self._target is None:
                return False
            current = snapshot(self.workspace)
            if not current.clean or current.source_sha != self._target \
                    or current.tree_sha256 != self._review.reviewed_tree_sha256:
                return False
            _, green = self._verified_regression(*self._regression)
            if green is None or green.tree_sha256 != current.tree_sha256:
                return False
            for scope in self.recipe.required_scopes:
                expected = [spec for spec in self.recipe.commands if spec.scope == scope]
                for spec in expected:
                    check = self._checks.get(spec.command_id)
                    if check is None or check.status != "passed":
                        return False
            for check in self._checks.values():
                pin = self.receipts.verify(check, self.recipe)
                if pin is not None and (pin.tree_sha256 != current.tree_sha256 or check.status != "passed"):
                    return False
            if self.recipe.build_mode is not None:
                if self._build is None:
                    return False
                self._verify_build(self._build, current)
            return True
        except ContributionError:
            return False
