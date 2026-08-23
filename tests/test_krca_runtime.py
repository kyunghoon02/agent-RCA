from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from incident_platform.errors import ContractViolation
from incident_platform.krca_runtime import load_krca_runtime_config


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "online-boutique-krca.yaml"


class KRCARuntimeConfigTests(unittest.TestCase):
    def test_online_boutique_profiles_render_live_promql_scope(self) -> None:
        config = load_krca_runtime_config(CONFIG)

        self.assertEqual(
            [profile.profile_id for profile in config.profiles],
            ["browse-and-cart-read", "cart-mutation", "checkout-full"],
        )
        checkout = config.profile("checkout-full")
        expression = config.query_spec.scoped_expression(
            config.query_spec.failure_rate_template,
            config.namespace,
            checkout.alerting_api,
        )
        self.assertEqual(
            expression,
            'agent_rca_api_failure_rate{namespace="online-boutique",'
            'service_name="frontend",span_name="POST"}',
        )
        self.assertNotIn("{{", expression)

        latency_expression = config.query_spec.scoped_expression(
            config.query_spec.latency_template,
            config.namespace,
            checkout.alerting_api,
        )
        self.assertEqual(
            latency_expression,
            'agent_rca_api_latency_p95_milliseconds{namespace="online-boutique",'
            'service_name="frontend",span_name="POST"} >= 0',
        )

    def test_online_boutique_profiles_cover_every_application_service(self) -> None:
        config = load_krca_runtime_config(CONFIG)

        covered_services = {
            service
            for profile in config.profiles
            for service in profile.resource_names
        }
        self.assertEqual(
            covered_services,
            {
                "adservice",
                "cartservice",
                "checkoutservice",
                "currencyservice",
                "emailservice",
                "frontend",
                "paymentservice",
                "productcatalogservice",
                "recommendationservice",
                "shippingservice",
            },
        )

    def test_online_boutique_profiles_cover_every_live_business_operation(self) -> None:
        config = load_krca_runtime_config(CONFIG)

        covered_apis = {
            (api.service, api.operation)
            for profile in config.profiles
            for dependency in profile.dependencies
            for api in (dependency.parent, dependency.child)
        }
        self.assertEqual(
            covered_apis,
            {
                ("adservice", "hipstershop.AdService/GetAds"),
                ("cartservice", "hipstershop.CartService/AddItem"),
                ("cartservice", "hipstershop.CartService/EmptyCart"),
                ("cartservice", "hipstershop.CartService/GetCart"),
                (
                    "checkoutservice",
                    "hipstershop.CheckoutService/PlaceOrder",
                ),
                (
                    "currencyservice",
                    "grpc.hipstershop.CurrencyService/Convert",
                ),
                (
                    "currencyservice",
                    "grpc.hipstershop.CurrencyService/GetSupportedCurrencies",
                ),
                (
                    "emailservice",
                    "/hipstershop.EmailService/SendOrderConfirmation",
                ),
                ("frontend", "GET"),
                ("frontend", "POST"),
                (
                    "paymentservice",
                    "grpc.hipstershop.PaymentService/Charge",
                ),
                (
                    "productcatalogservice",
                    "hipstershop.ProductCatalogService/GetProduct",
                ),
                (
                    "productcatalogservice",
                    "hipstershop.ProductCatalogService/ListProducts",
                ),
                (
                    "recommendationservice",
                    "/hipstershop.RecommendationService/ListRecommendations",
                ),
                (
                    "shippingservice",
                    "hipstershop.ShippingService/GetQuote",
                ),
                (
                    "shippingservice",
                    "hipstershop.ShippingService/ShipOrder",
                ),
            },
        )
    def test_dependency_cannot_escape_the_profile_scope(self) -> None:
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        raw["profiles"][0]["dependencies"][0]["child"]["service"] = "unknown"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "escapes resource scope"):
                load_krca_runtime_config(path)

    def test_disconnected_dependency_is_rejected(self) -> None:
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        dependency = copy.deepcopy(raw["profiles"][0]["dependencies"][1])
        dependency["parent"] = {
            "service": "adservice",
            "operation": "Disconnected",
        }
        raw["profiles"][0]["dependencies"][1] = dependency

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "disconnected"):
                load_krca_runtime_config(path)
