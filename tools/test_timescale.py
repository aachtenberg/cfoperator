"""Tests for the timescale_query read-only SQL guard.

No database required - the guard runs before any connection is opened,
and the connection itself is the second line of defense (read-only role
plus default_transaction_read_only=on), which these tests don't cover.
"""

import pytest

from tools.timescale import validate_readonly_sql


class TestValidateReadonlySql:
    def test_plain_select_passes(self):
        assert validate_readonly_sql("SELECT 1") == "SELECT 1"

    def test_with_cte_passes(self):
        sql = "WITH t AS (SELECT device, time FROM esp_status) SELECT device, count(*) FROM t GROUP BY device"
        assert validate_readonly_sql(sql) == sql

    def test_lowercase_and_whitespace_pass(self):
        assert validate_readonly_sql("  \n select * from esp_temperature limit 5 ") \
            == "select * from esp_temperature limit 5"

    def test_trailing_semicolon_stripped(self):
        assert validate_readonly_sql("SELECT 1;") == "SELECT 1"
        assert validate_readonly_sql("SELECT 1;; ;") == "SELECT 1"

    @pytest.mark.parametrize("sql", [
        "DELETE FROM esp_status",
        "INSERT INTO esp_status (device) VALUES ('x')",
        "UPDATE esp_status SET device = 'x'",
        "DROP TABLE esp_status",
        "TRUNCATE esp_status",
        "CREATE TABLE pwned (id int)",
        "GRANT ALL ON esp_status TO public",
        "EXPLAIN ANALYZE DELETE FROM esp_status",
        "VACUUM esp_status",
        "SET default_transaction_read_only = off",
        "CALL some_proc()",
    ])
    def test_non_select_rejected(self, sql):
        with pytest.raises(ValueError, match="read-only"):
            validate_readonly_sql(sql)

    @pytest.mark.parametrize("sql", [
        "SELECT 1; DELETE FROM esp_status",
        "SELECT 1; SELECT 2",
    ])
    def test_multiple_statements_rejected(self, sql):
        with pytest.raises(ValueError, match="one SELECT"):
            validate_readonly_sql(sql)

    def test_comment_hidden_write_rejected(self):
        # 'SELECT' only appearing inside a comment must not satisfy the prefix check
        with pytest.raises(ValueError):
            validate_readonly_sql("/* SELECT */ DELETE FROM esp_status")
        with pytest.raises(ValueError):
            validate_readonly_sql("-- SELECT 1\nDROP TABLE esp_status")

    def test_leading_comment_before_select_passes(self):
        assert validate_readonly_sql("-- top talkers\nSELECT 1").endswith("SELECT 1")

    def test_semicolon_hidden_by_comment_rejected(self):
        with pytest.raises(ValueError):
            validate_readonly_sql("SELECT 1 /* x */; DELETE FROM esp_status")

    @pytest.mark.parametrize("sql", ["", "   ", None])
    def test_empty_rejected(self, sql):
        with pytest.raises(ValueError, match="Empty"):
            validate_readonly_sql(sql)
