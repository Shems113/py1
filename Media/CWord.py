import numbers
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

import pandas as pd

from Common.EEnums import EActionType, EMediaType
from Common.Helpers.Dataframe import data_frame_from_csv
from Common.Helpers.File import get_target_filename_for_word
from Common.Helpers.System import IS_WINDOWS
from Media.CMedia import CMedia

win32 = __import__("win32com.client").client if IS_WINDOWS else None

def win32_required(func: Callable)-> Callable:
    """Décorateur pour restreindre l'exécution des méthodes Word à Windows."""
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not IS_WINDOWS or not self.word:
            raise NotImplementedError(
                f"L'action '{func.__name__}' sur les fichiers Word nécessite MS Word sous Windows."
            )
        return func(self, *args, **kwargs)
    return wrapper

class CWord(CMedia):
    def __init__(
        self,
        json_path: str | Path,
        action_type: EActionType
        )-> None:
        super().__init__(json_path, EMediaType.Word, action_type)
        self.word = None
        if IS_WINDOWS and win32:
            self.word = self.logger.log_elapsed_time(
                lambda: win32.Dispatch("Word.Application"), "Start Word application"
            )
            self.word.Visible = False

        self.df, self.value_by_bookmark = self.get_data()
        self.shuffle_df: bool = False

    def get_data(self)-> tuple[pd.DataFrame, dict[str, Any]]:
        if self.action_type == EActionType.Write and self.other_source_path:
            df = data_frame_from_csv(str(self.other_source_path), self.data_separator)
            return _compute_carrefour_bill_values(df[df["isSelected"] == 1].drop(columns=["isSelected"]))
        return pd.DataFrame(), {}

    def close(self)-> None:
        if IS_WINDOWS and self.word:
            self.logger.log_elapsed_time(self.word.Quit, "Quit Word application")

    @win32_required
    def read(self)-> None:
        self._word_action("Read bookmarks of ", [self._read_document_bookmarks])

    @win32_required
    def bookmark(self)-> None:
        self._word_action(
            "Bookmark ", [self._delete_document_bookmarks, self._add_document_bookmarks], file_format=16
        )

    @win32_required
    def write(self)-> None:
        actions = [self._read_document_bookmarks, self._write_document_bookmarks, self._delete_document_bookmarks]
        self._word_action("Write bookmarks of ", actions, file_format=17)

    def _word_action(
        self,
        label_prefix: str,
        document_actions: list[Callable[[Any], list[str] | str]],
        file_format: int | None = None
    )-> None:
        for file_path in self.get_source_files():
            self.logger.log_elapsed_time(
                lambda p=file_path: self._execute_on_document(p, document_actions, file_format),
                f"{label_prefix}{file_path}",
            )

    def _execute_on_document(
        self,
        word_path: str,
        actions: list[Callable[[Any], Any]],
        file_format: int | None
    )-> list[Any]:
        doc = self.word.Documents.Open(word_path)
        try:
            results = [action(doc) for action in actions]
            file_name = get_target_filename_for_word(word_path, file_format)
            if file_name and self.target_path:
                doc.SaveAs2(str(Path(self.target_path) / file_name), FileFormat=file_format)
            return results
        finally:
            doc.Close()

    def _read_document_bookmarks(self, doc: Any)-> list[str]:
        return [b.Name for b in doc.Bookmarks]

    def _delete_document_bookmarks(self, doc: Any)-> str:
        for b in list(doc.Bookmarks):
            b.Delete()
        return "DeleteBookmarks"

    def _add_document_bookmarks(self, doc: Any)-> str:
        for r in self.df.itertuples(index=False):
            is_table = getattr(r, "type", None) == "table"
            target_range = doc.Tables[int(r.value)].Range if is_table else self._find_range(doc, str(r.value))

            if target_range:
                self.logger.log(f"Add bookmark : {doc.Bookmarks.Add(str(r.name), target_range)}")
            else:
                self.logger.log(f"Not found text for bookmark {r.name} with value {r.value}")
        return "AddBookmarks"

    def _find_range(
        self,
        doc: Any,
        text: str
        )-> Any | None:
        rng = doc.Range()
        return rng if rng.Find.Execute(Text=text) else None

    def _write_document_bookmarks(self, doc: Any)-> str:
        for name, val in self.value_by_bookmark.items():
            if doc.Bookmarks.Exists(name):
                doc.Bookmarks(name).Range.Text = str(val)

        df = self.df.sample(frac=1).reset_index(drop=True) if self.shuffle_df else self.df

        if doc.Bookmarks.Exists("productTable"):
            table = doc.Bookmarks["productTable"].Range.Tables[0]
            last_range = table.Rows.First.Next.Range
            for i in range(len(df)):
                last_range = table.Rows.Add(last_range).Range
                for j in range(len(df.columns)):
                    table.Cell(3, j + 1).Range.Text = str(df.iat[i, j])

        self.shuffle_df = True
        return "WriteBookmarks"


def _compute_carrefour_bill_values(
    df: pd.DataFrame,
)-> tuple[pd.DataFrame, dict[str, Any]]:
    """Calcule le résumé de facturation Carrefour."""
    df_calc = df.assign(priceWithTva=df["price"] * df["tva"] / 100)
    bill_date = datetime.now().astimezone() + timedelta(days=-1, hours=-5)
    total = df_calc["price"].sum()
    tva_20 = df_calc[df_calc["tva"] == 20]["price"].sum()
    tva_5_5 = df_calc[df_calc["tva"] == 5.5]["price"].sum()

    values = {
        "billdDateTime1": bill_date.strftime("%d/%m/%Y à %Hh%M"),
        "billdDateTime2": (bill_date + timedelta(minutes=2)).strftime(
            "%d.%m.%Y %H:%M"
        ),
        "total": total,
        "montantCBPayé": total,
        "totalTVA20": tva_20,
        "totalTVA5_5": tva_5_5,
        "totalTVA": tva_20 + tva_5_5,
        "tva": df_calc["priceWithTva"].sum(),
        "tva20": df_calc[df_calc["tva"] == 20]["priceWithTva"].sum(),
        "tva5_5": df_calc[df_calc["tva"] == 5.5]["priceWithTva"].sum(),
    }

    df_res = df_calc.drop(columns=["priceWithTva"]).assign(
        tva=df_calc["tva"].astype(str) + "%"
    )
    formatted = {
        k: f"{round(v, 2)}€" if isinstance(v, numbers.Number) else v
        for k, v in values.items()
    }
    return df_res, formatted
