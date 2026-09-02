"""Security contract for Admin-provisioned Expert accounts."""

import unittest
from unittest.mock import ANY, patch

from fastapi import HTTPException

from api.routes import RegisterRequest, _require_admin, create_expert_account, router


class AdminUserProvisioningTests(unittest.TestCase):
    def test_public_registration_route_does_not_exist(self):
        routes = {(route.path, method) for route in router.routes for method in route.methods}
        self.assertNotIn(("/api/auth/register", "POST"), routes)
        self.assertIn(("/api/admin/users", "POST"), routes)

        create_route = next(
            route for route in router.routes
            if route.path == "/api/admin/users" and "POST" in route.methods
        )
        dependencies = {dependency.call for dependency in create_route.dependant.dependencies}
        self.assertIn(_require_admin, dependencies)

    def test_expert_cannot_pass_admin_authorization(self):
        with self.assertRaises(HTTPException) as forbidden:
            _require_admin({"id": 2, "name": "Expert", "email": "expert@example.com", "role": "expert"})
        self.assertEqual(forbidden.exception.status_code, 403)

    @patch("api.routes.register_expert")
    def test_admin_endpoint_always_creates_expert(self, register_expert_mock):
        register_expert_mock.return_value = {
            "id": 7,
            "name": "Chuyên gia A",
            "email": "expert-a@example.com",
            "role": "expert",
        }
        request = RegisterRequest(
            fullName="Chuyên gia A",
            email="expert-a@example.com",
            password="SafePassword!2026",
            confirmPassword="SafePassword!2026",
        )

        result = create_expert_account(
            request,
            {"id": 1, "name": "Admin", "email": "admin@example.com", "role": "admin"},
        )

        self.assertEqual(result["user"]["role"], "expert")
        register_expert_mock.assert_called_once_with(
            ANY,
            "Chuyên gia A",
            "expert-a@example.com",
            "SafePassword!2026",
        )

    def test_password_confirmation_is_checked(self):
        request = RegisterRequest(
            fullName="Chuyên gia A",
            email="expert-a@example.com",
            password="SafePassword!2026",
            confirmPassword="DifferentPassword!2026",
        )
        with self.assertRaises(HTTPException) as invalid:
            create_expert_account(
                request,
                {"id": 1, "name": "Admin", "email": "admin@example.com", "role": "admin"},
            )
        self.assertEqual(invalid.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
