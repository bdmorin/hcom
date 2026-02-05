//! Centralized path resolution for hcom
//!
//! Single source of truth for all hcom directory and file paths.
//! Respects HCOM_DIR env var for worktrees/dev, falls back to ~/.hcom.

use std::path::PathBuf;
use crate::config::Config;

/// Get the hcom base directory.
///
/// Uses centralized Config (HCOM_DIR env var or ~/.hcom fallback).
pub fn hcom_dir() -> PathBuf {
    Config::get().hcom_dir
}

/// Get the database path (hcom_dir/hcom.db)
pub fn db_path() -> PathBuf {
    hcom_dir().join("hcom.db")
}

/// Get the log file path (hcom_dir/.tmp/logs/hcom.log)
pub fn log_path() -> PathBuf {
    hcom_dir().join(".tmp").join("logs").join("hcom.log")
}

/// Get the daemon socket path (hcom_dir/hcomd.sock)
pub fn socket_path() -> PathBuf {
    hcom_dir().join("hcomd.sock")
}

/// Get the daemon version file path (hcom_dir/.tmp/daemon.version)
/// Written by daemon on startup, read by client to detect version mismatch.
pub fn daemon_version_path() -> PathBuf {
    hcom_dir().join(".tmp").join("daemon.version")
}

/// Get the daemon PID file path (hcom_dir/hcomd.pid)
pub fn pid_path() -> PathBuf {
    hcom_dir().join("hcomd.pid")
}

