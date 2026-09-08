# fina-risk schemas

Each JSON snippet below is materialized as a standalone JSON Schema (Draft 2020-12) file in this directory. Load the file before constructing or validating any object:

| Layer | Schema file | $id |
|---|---|---|
| HOT | `process-cache.schema.json` | process-cache.schema.json |
| HOT | `path-cube.schema.json` | path-cube.schema.json |
| HOT | `state-cube.schema.json` | state-cube.schema.json |
| HOT | `aad-tape.schema.json` | aad-tape.schema.json |
| WARM | `simulation-universe.schema.json` | simulation-universe.schema.json |
| WARM | `job-status.schema.json` | job-status.schema.json |
| WARM | `risk-cache.schema.json` | risk-cache.schema.json |
| WARM | `payoff-graph.schema.json` | payoff-graph.schema.json |
| COLD | `path-cube-archive.schema.json` | path-cube-archive.schema.json |
| COLD | `risk-cube.schema.json` | risk-cube.schema.json |
| COLD | `risk-cell.schema.json` | risk-cell.schema.json |
| COLD | `pnl-explain.schema.json` | pnl-explain.schema.json |
| COLD | `portfolio-risk.schema.json` | portfolio-risk.schema.json |

The schemas are intentionally counterparty-neutral, product-neutral, and portfolio-neutral. They are defined around storage boundaries, not computation boundaries.

For the fina-risk architecture, I'd strongly recommend defining schemas around storage boundaries, not computation boundaries.

In practice:

HOT
 = in-memory / GPU-resident
 = mutable
 = frequent access

WARM
 = Redis
 = cached
 = recoverable

COLD
 = Parquet / Object Storage
 = immutable history


The schemas below are intentionally counterparty-neutral, product-neutral, and portfolio-neutral.

Hot Data Schemas
Process Cache
{
  "$id": "process-cache.schema.json",
  "type": "object",
  "required": [
    "process_id",
    "model_type",
    "market_version"
  ],
  "properties": {
    "process_id": {
      "type": "string"
    },
    "model_type": {
      "type": "string"
    },
    "market_version": {
      "type": "string"
    },
    "risk_factor_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "curve_refs": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "surface_refs": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "correlation_ref": {
      "type": "string"
    },
    "created_at": {
      "type": "string",
      "format": "date-time"
    }
  }
}

Path Cube Metadata
{
  "$id": "path-cube.schema.json",
  "type": "object",
  "required": [
    "cube_id",
    "universe_id",
    "path_count",
    "time_steps",
    "factor_count"
  ],
  "properties": {
    "cube_id": {
      "type": "string"
    },
    "universe_id": {
      "type": "string"
    },
    "simulation_model": {
      "type": "string"
    },
    "path_count": {
      "type": "integer"
    },
    "time_steps": {
      "type": "integer"
    },
    "factor_count": {
      "type": "integer"
    },
    "precision": {
      "enum": [
        "f32",
        "f64"
      ]
    },
    "storage_backend": {
      "enum": [
        "gpu",
        "ram",
        "shared_memory"
      ]
    },
    "memory_bytes": {
      "type": "integer"
    }
  }
}

State Cube
{
  "$id": "state-cube.schema.json",
  "type": "object",
  "required": [
    "state_cube_id",
    "path_cube_id"
  ],
  "properties": {
    "state_cube_id": {
      "type": "string"
    },
    "path_cube_id": {
      "type": "string"
    },
    "states": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "dimensions": {
      "type": "object",
      "properties": {
        "paths": {
          "type": "integer"
        },
        "dates": {
          "type": "integer"
        },
        "states": {
          "type": "integer"
        }
      }
    }
  }
}

AAD Tape Metadata
{
  "$id": "aad-tape.schema.json",
  "type": "object",
  "required": [
    "tape_id",
    "valuation_graph_id"
  ],
  "properties": {
    "tape_id": {
      "type": "string"
    },
    "valuation_graph_id": {
      "type": "string"
    },
    "node_count": {
      "type": "integer"
    },
    "edge_count": {
      "type": "integer"
    },
    "adjoint_enabled": {
      "type": "boolean"
    },
    "tape_size_bytes": {
      "type": "integer"
    }
  }
}

Warm Data Schemas
Universe Cache
{
  "$id": "simulation-universe.schema.json",
  "type": "object",
  "required": [
    "universe_id",
    "market_version"
  ],
  "properties": {
    "universe_id": {
      "type": "string"
    },
    "market_version": {
      "type": "string"
    },
    "underlyings": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "model_type": {
      "type": "string"
    },
    "calendar_id": {
      "type": "string"
    },
    "observation_schedule_hash": {
      "type": "string"
    },
    "trade_ids": {
      "type": "array",
      "items": {
        "type": "string"
      }
    }
  }
}

Job Cache
{
  "$id": "job-status.schema.json",
  "type": "object",
  "required": [
    "job_id",
    "status"
  ],
  "properties": {
    "job_id": {
      "type": "string"
    },
    "status": {
      "enum": [
        "PENDING",
        "RUNNING",
        "COMPLETED",
        "FAILED"
      ]
    },
    "submitted_at": {
      "format": "date-time",
      "type": "string"
    },
    "started_at": {
      "format": "date-time",
      "type": "string"
    },
    "completed_at": {
      "format": "date-time",
      "type": "string"
    },
    "worker_id": {
      "type": "string"
    }
  }
}

Risk Cache
{
  "$id": "risk-cache.schema.json",
  "type": "object",
  "required": [
    "risk_run_id",
    "market_version"
  ],
  "properties": {
    "risk_run_id": {
      "type": "string"
    },
    "market_version": {
      "type": "string"
    },
    "risk_cube_ref": {
      "type": "string"
    },
    "method": {
      "enum": [
        "AAD",
        "PATHWISE",
        "LRM",
        "FD"
      ]
    },
    "generated_at": {
      "type": "string",
      "format": "date-time"
    }
  }
}

Graph Cache
{
  "$id": "payoff-graph.schema.json",
  "type": "object",
  "required": [
    "graph_id",
    "graph_type"
  ],
  "properties": {
    "graph_id": {
      "type": "string"
    },
    "graph_type": {
      "enum": [
        "PAYOFF",
        "STATE",
        "AAD",
        "VALUATION"
      ]
    },
    "root_node": {
      "type": "string"
    },
    "node_count": {
      "type": "integer"
    },
    "graph_hash": {
      "type": "string"
    }
  }
}

Cold Data Schemas
Path Cube Archive

Metadata only.

Actual matrix stored in Parquet.

{
  "$id": "path-cube-archive.schema.json",
  "type": "object",
  "required": [
    "archive_id",
    "cube_id"
  ],
  "properties": {
    "archive_id": {
      "type": "string"
    },
    "cube_id": {
      "type": "string"
    },
    "object_uri": {
      "type": "string"
    },
    "compression": {
      "enum": [
        "zstd",
        "snappy",
        "gzip"
      ]
    },
    "row_count": {
      "type": "integer"
    }
  }
}

Risk Cube Archive
{
  "$id": "risk-cube.schema.json",
  "type": "object",
  "required": [
    "cube_id",
    "valuation"
  ],
  "properties": {
    "cube_id": {
      "type": "string"
    },
    "valuation": {
      "type": "object",
      "properties": {
        "pv": {
          "type": "number"
        },
        "currency": {
          "type": "string"
        }
      }
    },
    "axes": {
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "aggregation_level": {
      "enum": [
        "TRADE",
        "LEG",
        "FEATURE",
        "PORTFOLIO"
      ]
    },
    "cell_count": {
      "type": "integer"
    }
  }
}

Risk Cube Cell
{
  "$id": "risk-cell.schema.json",
  "type": "object",
  "required": [
    "risk_factor_id",
    "measure",
    "value"
  ],
  "properties": {
    "risk_factor_id": {
      "type": "string"
    },
    "measure": {
      "type": "string"
    },
    "value": {
      "type": "number"
    },
    "currency": {
      "type": "string"
    },
    "method": {
      "enum": [
        "AAD",
        "PATHWISE",
        "LRM",
        "FD"
      ]
    },
    "fallback_reason": {
      "type": [
        "string",
        "null"
      ]
    }
  }
}

PnL Archive
{
  "$id": "pnl-explain.schema.json",
  "type": "object",
  "required": [
    "valuation_date",
    "actual_pnl"
  ],
  "properties": {
    "valuation_date": {
      "type": "string",
      "format": "date"
    },
    "actual_pnl": {
      "type": "number"
    },
    "forecast_pnl": {
      "type": "number"
    },
    "unexplained_pnl": {
      "type": "number"
    },
    "components": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "factor_id": {
            "type": "string"
          },
          "measure": {
            "type": "string"
          },
          "contribution": {
            "type": "number"
          }
        }
      }
    }
  }
}

Portfolio Risk Archive
{
  "$id": "portfolio-risk.schema.json",
  "type": "object",
  "required": [
    "portfolio_id",
    "valuation_timestamp"
  ],
  "properties": {
    "portfolio_id": {
      "type": "string"
    },
    "valuation_timestamp": {
      "type": "string",
      "format": "date-time"
    },
    "pv": {
      "type": "number"
    },
    "currency": {
      "type": "string"
    },
    "risk_cube_ref": {
      "type": "string"
    },
    "trade_count": {
      "type": "integer"
    }
  }
}

Recommended Physical Storage Mapping
HOT (RAM / GPU)
├─ ProcessCache
├─ PathCube
├─ StateCube
└─ AadTape

WARM (Redis)
├─ UniverseCache
├─ GraphCache
├─ RiskCache
└─ JobCache

COLD (Parquet + Object Storage)
├─ PathCubeArchive
├─ RiskCube
├─ RiskCell
├─ PnLExplain
└─ PortfolioRisk


This separation aligns well with GPU-accelerated Monte Carlo/AAD workloads because only objects needed during valuation remain in HOT memory, Redis stores lightweight recoverable metadata and coordination state, and large path/risk artifacts live in compressed Parquet storage for replay, audit, explainability, and model validation.
