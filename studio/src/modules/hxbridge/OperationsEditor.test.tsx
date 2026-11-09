/**
 * Saved queries for a database query connector (2026-11-08).
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import OperationsEditor, { queryProblem } from "./OperationsEditor";

describe("queryProblem", () => {
  it("accepts one read and refuses what writes or hides", () => {
    expect(queryProblem("SELECT id FROM t WHERE email = :email")).toBeNull();
    expect(queryProblem("SELECT replace(a, 'delete', '') FROM t;")).toBeNull();
    expect(queryProblem("DELETE FROM t")).toMatch(/SELECT or WITH/);
    expect(queryProblem("SELECT 1; DROP TABLE t")).toMatch(/One statement/);
    expect(queryProblem("WITH d AS (DELETE FROM t RETURNING 1) SELECT 1")).toMatch(/DELETE/);
    expect(queryProblem("SELECT 1 -- x")).toMatch(/comments/);
  });
});

describe("OperationsEditor for sql_query", () => {
  it("writes saved queries into the config and keeps the rest", () => {
    const onChange = vi.fn();
    render(<OperationsEditor connectorType="sql_query" configText={JSON.stringify({ engine: "mysql", host: "db.example.com" })}
      onChange={onChange} />);
    fireEvent.click(screen.getByText("+ Add query"));
    const cfg = JSON.parse(onChange.mock.calls[0][0]);
    expect(cfg.engine).toBe("mysql");
    expect(cfg.operations).toEqual([{ key: "query_1", label: "", sql: "SELECT " }]);
  });

  it("offers the database settings when none are there", () => {
    const onChange = vi.fn();
    render(<OperationsEditor connectorType="sql_query" configText="{}" onChange={onChange} />);
    fireEvent.click(screen.getByText("Fill database settings"));
    expect(JSON.parse(onChange.mock.calls[0][0]).engine).toBe("postgresql");
  });

  it("shows the problem with a query", () => {
    render(<OperationsEditor connectorType="sql_query" onChange={() => {}}
      configText={JSON.stringify({ engine: "postgresql", operations: [{ key: "w", sql: "UPDATE t SET a = 1" }] })} />);
    expect(screen.getByText(/SELECT or WITH/)).toBeTruthy();
  });
});
