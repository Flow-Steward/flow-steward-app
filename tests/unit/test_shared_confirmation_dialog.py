"""The shared confirmation dialog remains accessible."""


class TestSharedConfirmationDialog:
    def test_the_shared_primitive_exists_and_is_a_real_dialog(self):
        from pathlib import Path

        component = Path("assets/frontend/src/components/shared/confirm-dialog.tsx").read_text(
            encoding="utf-8"
        )

        primitive = Path(
            "assets/frontend/src/components/shared/design-system-overlays.tsx"
        ).read_text(encoding="utf-8")

        assert "<DsDialog" in component
        assert 'role="dialog"' in component
        assert "title={title}" in component, (
            "automation and assistive tech need an accessible name to target"
        )
        assert "<dialog" in primitive
        assert 'aria-modal="true"' in primitive
        assert "aria-labelledby={titleId}" in primitive
        assert "createPortal(" in primitive
        assert "document.body," in primitive
