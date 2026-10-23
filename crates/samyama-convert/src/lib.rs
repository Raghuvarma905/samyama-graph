//! Convert graph file formats to and from Samyama's `.sgsnap`, offline, with a
//! loss report (#1905, #1908).
//!
//! This crate is the converter on its own, for programs that do not want the
//! rest of the engine's API in view. The code is the engine's
//! [`samyama::convert`] module, re-exported unchanged, so `samyama convert`
//! and this crate always do the same thing: the `samyama` binary cannot depend
//! on a crate that depends on the engine, so the code lives there.
//!
//! ```no_run
//! use samyama_convert::{convert, Options};
//!
//! let report = convert("in.sgsnap".as_ref(), "out.sgsnap".as_ref(), &Options::default())?;
//! assert!(report.lossless());
//! # Ok::<(), samyama_convert::ConvertError>(())
//! ```

pub use samyama::convert::*;
