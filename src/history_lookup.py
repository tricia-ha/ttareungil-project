"""Indexed equivalent of immutable many-to-one history-table left merges."""

import pandas as pd
from pandas.api.types import is_dtype_equal


class IndexedHistoryTable:
    def __init__(self, frame, keys):
        self.keys = list(keys)
        self.frame = frame
        self.values = [column for column in frame if column not in self.keys]
        if frame.duplicated(self.keys).any():
            raise pd.errors.MergeError("History keys must be unique (many_to_one).")
        self.indexed = frame.set_index(self.keys)
        # Build the immutable index's hash engine once, not for every two-row
        # request. is_unique also checks the constructor invariant above.
        self.indexed.index.is_unique
        self.has_null_keys = frame[self.keys].isna().any().any()

    def join(self, frame):
        # Preserve pandas merge semantics for unusual external inputs. Normal
        # service frames have non-null, same-dtype, non-overlapping keys.
        if (
            self.has_null_keys
            or frame[self.keys].isna().any().any()
            or any(
                not is_dtype_equal(frame[k].dtype, self.frame[k].dtype)
                for k in self.keys
            )
            or any(column in frame for column in self.values)
        ):
            return frame.merge(
                self.frame, on=self.keys, how="left", validate="many_to_one"
            )
        key = (
            pd.Index(frame[self.keys[0]])
            if len(self.keys) == 1
            else pd.MultiIndex.from_frame(frame[self.keys])
        )
        selected = self.indexed.reindex(key).reset_index(drop=True)
        return pd.concat([frame.reset_index(drop=True), selected], axis=1)
