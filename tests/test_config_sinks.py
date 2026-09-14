import unittest

from gps_gateway.config import Config
from gps_gateway.sinks import BatchingSink, LogSink, PostgresSink, build_sink


class TestConfig(unittest.TestCase):
    def test_defaults_when_environment_is_empty(self):
        config = Config.from_env({})
        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 5023)
        self.assertEqual(config.sink, "postgres")
        self.assertEqual(config.batch_size, 500)
        self.assertEqual(config.flush_interval_s, 10.0)
        self.assertEqual(config.queue_max, 10_000)

    def test_reads_overrides_and_coerces_types(self):
        config = Config.from_env(
            {"GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": "6000", "GATEWAY_SINK": "log"}
        )
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 6000)
        self.assertIsInstance(config.port, int)
        self.assertEqual(config.sink, "log")

    def test_reads_batching_overrides(self):
        config = Config.from_env(
            {
                "GATEWAY_BATCH_SIZE": "200",
                "GATEWAY_FLUSH_INTERVAL": "2.5",
                "GATEWAY_QUEUE_MAX": "50",
            }
        )
        self.assertEqual(
            (config.batch_size, config.flush_interval_s, config.queue_max), (200, 2.5, 50)
        )


class TestBuildSink(unittest.TestCase):
    def test_builds_a_log_sink(self):
        self.assertIsInstance(build_sink(Config(sink="log")), LogSink)

    def test_builds_a_batching_postgres_sink_by_default(self):
        sink = build_sink(Config())
        self.assertIsInstance(sink, BatchingSink)
        self.assertIsInstance(sink.inner, PostgresSink)

    def test_batch_size_one_writes_straight_to_postgres(self):
        self.assertIsInstance(build_sink(Config(batch_size=1)), PostgresSink)

    def test_rejects_an_unknown_sink_name(self):
        # A typo in GATEWAY_SINK used to silently fall through to Postgres.
        with self.assertRaises(ValueError):
            build_sink(Config(sink="kafka"))


if __name__ == "__main__":
    unittest.main()
