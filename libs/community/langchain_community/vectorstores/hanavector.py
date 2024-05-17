"""SAP HANA Cloud Vector Engine"""
from __future__ import annotations

import importlib.util
import json
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
)

import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.runnables.config import run_in_executor
from langchain_core.vectorstores import VectorStore

from langchain_community.vectorstores.utils import (
    DistanceStrategy,
    maximal_marginal_relevance,
)

if TYPE_CHECKING:
    from hdbcli import dbapi

HANA_DISTANCE_FUNCTION: dict = {
    DistanceStrategy.COSINE: ("COSINE_SIMILARITY", "DESC"),
    DistanceStrategy.EUCLIDEAN_DISTANCE: ("L2DISTANCE", "ASC"),
}

COMPARISONS_TO_SQL = {
    "$eq": "=",
    "$ne": "<>",
    "$lt": "<",
    "$lte": "<=",
    "$gt": ">",
    "$gte": ">=",
}

IN_OPERATORS_TO_SQL = {
    "$in": "IN",
    "$nin": "NOT IN",
}

BETWEEN_OPERATOR = "$between"

LIKE_OPERATOR = "$like"

LOGICAL_OPERATORS_TO_SQL = {"$and": "AND", "$or": "OR"}


SUPPORTED_SQL_DATATYPES = {"NVARCHAR": "NVARCHAR(5000)", "INT": "INTEGER", "INTEGER": "INTEGER", "DOUBLE": "DOUBLE"}

default_distance_strategy = DistanceStrategy.COSINE
default_table_name: str = "EMBEDDINGS"
default_content_column: str = "VEC_TEXT"
default_metadata_column: str = "VEC_META"
default_vector_column: str = "VEC_VECTOR"
default_vector_column_length: int = -1  # -1 means dynamic length


class HanaDB(VectorStore):
    """SAP HANA Cloud Vector Engine

    The prerequisite for using this class is the installation of the ``hdbcli``
    Python package.

    The HanaDB vectorstore can be created by providing an embedding function and
    an existing database connection. Optionally, the names of the table and the
    columns to use.
    """

    def __init__(
        self,
        connection: dbapi.Connection,
        embedding: Embeddings,
        distance_strategy: DistanceStrategy = default_distance_strategy,
        table_name: str = default_table_name,
        content_column: str = default_content_column,
        metadata_column: str = default_metadata_column,
        vector_column: str = default_vector_column,
        vector_column_length: int = default_vector_column_length,
        data_columns: dict = None,
    ):
        # Check if the hdbcli package is installed
        if importlib.util.find_spec("hdbcli") is None:
            raise ImportError(
                "Could not import hdbcli python package. "
                "Please install it with `pip install hdbcli`."
            )

        valid_distance = False
        for key in HANA_DISTANCE_FUNCTION.keys():
            if key is distance_strategy:
                valid_distance = True
        if not valid_distance:
            raise ValueError(
                "Unsupported distance_strategy: {}".format(distance_strategy)
            )

        self.connection = connection
        self.embedding = embedding
        self.distance_strategy = distance_strategy
        self.table_name = HanaDB._sanitize_name(table_name)
        self.content_column = HanaDB._sanitize_name(content_column)
        self.metadata_column = HanaDB._sanitize_name(metadata_column)
        self.vector_column = HanaDB._sanitize_name(vector_column)
        self.vector_column_length = HanaDB._sanitize_int(vector_column_length)
        self.data_columns = data_columns
        self.read_only = self._is_dbobject_readonly(self.table_name)

        # Check if the table exists, and eventually create it
        if not self._table_exists(self.table_name):
            # if data_columns are configured, generate a DDL for that column. The string is suffixed with , when not empty
            data_columns_sql_str = ''
            if self.data_columns:
                for key in self.data_columns:
                    if not self.data_columns[key].upper() in SUPPORTED_SQL_DATATYPES.keys():
                        raise ValueError(f"Unsupported SQL datatype: {self.data_columns[key]}. Use {[str(key) for key in SUPPORTED_SQL_DATATYPES.keys()]}")
                    else:
                        data_columns_sql_str += f'''"{HanaDB._sanitize_name(key)}" {SUPPORTED_SQL_DATATYPES[self.data_columns[key]]}, '''
            sql_str = (
                f'CREATE TABLE "{self.table_name}"('
                f'{data_columns_sql_str}' # "col1" INT, "col2" DOUBLE,
                f'"{self.content_column}" NCLOB, '
                f'"{self.metadata_column}" NCLOB, '
                f'"{self.vector_column}" REAL_VECTOR '
            )
            if self.vector_column_length == -1:
                sql_str += ");"
            else:
                sql_str += f"({self.vector_column_length}));"

            try:
                cur = self.connection.cursor()
                cur.execute(sql_str)
                self.read_only = False
            finally:
                cur.close()
        # The table does exist. Check if data columns need to be added.
        else:
            if self.data_columns and not self.read_only:
                existing_data_columns = HanaDB._get_data_columns(self, self.table_name, self.content_column, self.metadata_column, self.vector_column)
                new_data_columns = list(self.data_columns.keys() - existing_data_columns.keys())
                if len(new_data_columns) > 0:
                    sql_str_alter = f'ALTER TABLE {self.table_name} ADD ( '
                    sql_str_update = f'UPDATE {self.table_name} SET '
                    for key in new_data_columns:
                        if not self.data_columns[key].upper() in SUPPORTED_SQL_DATATYPES.keys():
                            raise ValueError(f"Unsupported SQL datatype: {self.data_columns[key]}. Use {[str(key) for key in SUPPORTED_SQL_DATATYPES.keys()]}")
                        else:
                            sanitized_name = HanaDB._sanitize_name(key)
                            sql_str_alter += f' "{sanitized_name}" {SUPPORTED_SQL_DATATYPES[self.data_columns[key]]},'
                            sql_str_update += f''' "{sanitized_name}" = JSON_VALUE({self.metadata_column}, '$.{sanitized_name}'),'''
                    sql_str_alter = sql_str_alter[0:-1] + ')'
                    sql_str_update = sql_str_update[0:-1]
                    try:
                        self.connection.setautocommit(False)
                        cur = self.connection.cursor()
                        cur.execute(sql_str_alter)
                        cur.execute(sql_str_update)
                        self.connection.commit()
                    except:
                        self.connection.rollback()
                    finally:
                        cur.close()
                        self.connection.setautocommit(True)

        # Check if the needed columns exist and have the correct type
        self._check_column(self.table_name, self.content_column, ["NCLOB", "NVARCHAR"])
        self._check_column(self.table_name, self.metadata_column, ["NCLOB", "NVARCHAR"])
        self._check_column(
            self.table_name,
            self.vector_column,
            ["REAL_VECTOR"],
            self.vector_column_length,
        )
        # MF: and get the additional data columns from the table. this could be merged with the check columns above
        self.data_columns = HanaDB._get_data_columns(self, self.table_name, self.content_column, self.metadata_column, self.vector_column)

    def _table_exists(self, table_name) -> bool:  # type: ignore[no-untyped-def]
        sql_str = (
            "SELECT COUNT(*) FROM ("
            "SELECT TABLE_NAME AS ENT FROM SYS.TABLES WHERE SCHEMA_NAME = CURRENT_SCHEMA AND TABLE_NAME = ? UNION "
            "SELECT VIEW_NAME AS ENT FROM SYS.VIEWS WHERE SCHEMA_NAME = CURRENT_SCHEMA AND VIEW_NAME = ?)"
        )
        try:
            cur = self.connection.cursor()
            cur.execute(sql_str, (table_name, table_name))
            if cur.has_result_set():
                rows = cur.fetchall()
                if rows[0][0] == 1:
                    return True
        finally:
            cur.close()
        return False

    def _check_column(self, table_name, column_name, column_type, column_length=None):  # type: ignore[no-untyped-def]
        sql_str = (
            "SELECT DATA_TYPE_NAME, LENGTH FROM ("
            "SELECT DATA_TYPE_NAME, LENGTH, COLUMN_NAME, TABLE_NAME AS NAME, SCHEMA_NAME FROM SYS.TABLE_COLUMNS UNION "
            "SELECT DATA_TYPE_NAME, LENGTH, COLUMN_NAME, VIEW_NAME AS NAME, SCHEMA_NAME FROM SYS.VIEW_COLUMNS)"
            "WHERE SCHEMA_NAME = CURRENT_SCHEMA AND NAME = ? AND COLUMN_NAME = ?"
        )
        try:
            cur = self.connection.cursor()
            cur.execute(sql_str, (table_name, column_name))
            if cur.has_result_set():
                rows = cur.fetchall()
                if len(rows) == 0:
                    raise AttributeError(f"Column {column_name} does not exist")
                # Check data type
                if rows[0][0] not in column_type:
                    raise AttributeError(
                        f"Column {column_name} has the wrong type: {rows[0][0]}"
                    )
                # Check length, if parameter was provided
                # column_length is always set to default None, so it is always checked, even if a table/view was reused
                if column_length is not None:
                    if rows[0][1] != column_length:
                        raise AttributeError(
                            f"Column {column_name} has the wrong length: {rows[0][1]}"
                        )
            else:
                raise AttributeError(f"Column {column_name} does not exist")
        finally:
            cur.close()

    def _is_dbobject_readonly(self, table_name) -> bool:
        sql_str = (
            "SELECT COUNT(*) FROM SYS.TABLES WHERE SCHEMA_NAME = CURRENT_SCHEMA AND TABLE_NAME = ?"
        )
        try:
            cur = self.connection.cursor()
            cur.execute(sql_str, (table_name))
            if cur.has_result_set():
                rows = cur.fetchall()
                if rows[0][0] == 1:
                    return False
        finally:
            cur.close()
        return True

    def _get_data_columns(self, table_name, content_column, metadata_column, vector_column) -> dict: # type: ignore[no-untyped-def]
        sql_str = (
            "SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH FROM ("
            "SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, TABLE_NAME AS NAME, SCHEMA_NAME FROM SYS.TABLE_COLUMNS UNION "
            "SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, VIEW_NAME AS NAME, SCHEMA_NAME FROM SYS.VIEW_COLUMNS "
            ") WHERE SCHEMA_NAME = CURRENT_SCHEMA AND NAME = ? AND COLUMN_NAME NOT IN (?, ?, ?)"
        )
        data_columns = {}
        try:
            cur = self.connection.cursor()
            cur.execute(sql_str, (table_name, content_column, metadata_column, vector_column))
            if cur.has_result_set():
                rows = cur.fetchall()
                if len(rows) > 0:
                    for row in rows:
                        data_columns[row[0]] = row[1]
        finally:
            cur.close()
        return data_columns

    @property
    def embeddings(self) -> Embeddings:
        return self.embedding

    def _sanitize_name(input_str: str) -> str:  # type: ignore[misc]
        # Remove characters that are not alphanumeric or underscores
        return re.sub(r"[^a-zA-Z0-9_]", "", input_str)

    def _sanitize_int(input_int: any) -> int:  # type: ignore[valid-type]
        value = int(str(input_int))
        if value < -1:
            raise ValueError(f"Value ({value}) must not be smaller than -1")
        return int(str(input_int))

    def _sanitize_list_float(embedding: List[float]) -> List[float]:  # type: ignore[misc]
        for value in embedding:
            if not isinstance(value, float):
                raise ValueError(f"Value ({value}) does not have type float")
        return embedding

    # Compile pattern only once, for better performance
    _compiled_pattern = re.compile("^[_a-zA-Z][_a-zA-Z0-9]*$")

    def _sanitize_metadata_keys(metadata: dict) -> dict:  # type: ignore[misc]
        for key in metadata.keys():
            if not HanaDB._compiled_pattern.match(key):
                raise ValueError(f"Invalid metadata key {key}")

        return metadata

    def add_texts(  # type: ignore[override]
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        embeddings: Optional[List[List[float]]] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Add more texts to the vectorstore.

        Args:
            texts (Iterable[str]): Iterable of strings/text to add to the vectorstore.
            metadatas (Optional[List[dict]], optional): Optional list of metadatas.
                Defaults to None.
            embeddings (Optional[List[List[float]]], optional): Optional pre-generated
                embeddings. Defaults to None.

        Returns:
            List[str]: empty list
        """
        # If the vectorstore is based on a view, we can't insert
        if self.read_only:
            raise Warning('The database object that contains the data is read only. Most probably it is a view.')

        # Create all embeddings of the texts beforehand to improve performance
        if embeddings is None:
            embeddings = self.embedding.embed_documents(list(texts))

        cur = self.connection.cursor()
        try:
            # Insert data into the table
            for i, text in enumerate(texts):
                # Use provided values by default or fallback
                metadata = metadatas[i] if metadatas else {}
                # Get the string of all column names, add the right number of placeholders, copy values from metadata if exists
                col_str = ',' + '","'.join(self.data_columns.keys()).join(('"','"')) if len(self.data_columns) > 0 else ''
                param_str = ',?' * len(self.data_columns)
                data_values = []
                for data_column in self.data_columns:
                    data_values.append(metadata[data_column] if data_column in metadata.keys() else None)
                embedding = (
                    embeddings[i]
                    if embeddings
                    else self.embedding.embed_documents([text])[0]
                )
                sql_str = (
                    f'INSERT INTO "{self.table_name}" ("{self.content_column}", '
                    f'"{self.metadata_column}", "{self.vector_column}" {col_str}) '
                    f"VALUES (?, ?, TO_REAL_VECTOR (?) {param_str});"
                )
                cur.execute(
                    sql_str,
                    (
                        text,
                        json.dumps(HanaDB._sanitize_metadata_keys(metadata)),
                        f"[{','.join(map(str, embedding))}]"
                    ) + tuple(data_values)
                )
        finally:
            cur.close()
        return []

    @classmethod
    def from_texts(  # type: ignore[no-untyped-def, override]
        cls: Type[HanaDB],
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        connection: dbapi.Connection = None,
        distance_strategy: DistanceStrategy = default_distance_strategy,
        table_name: str = default_table_name,
        content_column: str = default_content_column,
        metadata_column: str = default_metadata_column,
        vector_column: str = default_vector_column,
        vector_column_length: int = default_vector_column_length,
        data_columns: dict = None,
    ):
        """Create a HanaDB instance from raw documents.
        This is a user-friendly interface that:
            1. Embeds documents.
            2. Creates a table if it does not yet exist.
            3. Adds the documents to the table.
        This is intended to be a quick way to get started.
        """

        instance = cls(
            connection=connection,
            embedding=embedding,
            distance_strategy=distance_strategy,
            table_name=table_name,
            content_column=content_column,
            metadata_column=metadata_column,
            vector_column=vector_column,
            vector_column_length=vector_column_length,  # -1 means dynamic length
            data_columns=data_columns,
        )
        instance.add_texts(texts, metadatas)
        return instance

    def similarity_search(  # type: ignore[override]
        self, query: str, k: int = 4, filter: Optional[dict] = None
    ) -> List[Document]:
        """Return docs most similar to query.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filter: A dictionary of metadata fields and values to filter by.
                    Defaults to None.

        Returns:
            List of Documents most similar to the query
        """
        docs_and_scores = self.similarity_search_with_score(
            query=query, k=k, filter=filter
        )
        return [doc for doc, _ in docs_and_scores]

    def similarity_search_with_score(
        self, query: str, k: int = 4, filter: Optional[dict] = None
    ) -> List[Tuple[Document, float]]:
        """Return documents and score values most similar to query.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filter: A dictionary of metadata fields and values to filter by.
                    Defaults to None.

        Returns:
            List of tuples (containing a Document and a score) that are
            most similar to the query
        """
        embedding = self.embedding.embed_query(query)
        return self.similarity_search_with_score_by_vector(
            embedding=embedding, k=k, filter=filter
        )

    def similarity_search_with_score_and_vector_by_vector(
        self, embedding: List[float], k: int = 4, filter: Optional[dict] = None
    ) -> List[Tuple[Document, float, List[float]]]:
        """Return docs most similar to the given embedding.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filter: A dictionary of metadata fields and values to filter by.
                    Defaults to None.

        Returns:
            List of Documents most similar to the query and
            score and the document's embedding vector for each
        """
        result = []
        k = HanaDB._sanitize_int(k)
        embedding = HanaDB._sanitize_list_float(embedding)
        distance_func_name = HANA_DISTANCE_FUNCTION[self.distance_strategy][0]
        embedding_as_str = ",".join(map(str, embedding))
        sql_str = (
            f"SELECT TOP {k}"
            f'  "{self.content_column}", '  # row[0]
            f'  "{self.metadata_column}", '  # row[1]
            f'  TO_NVARCHAR("{self.vector_column}"), '  # row[2]
            f'  {distance_func_name}("{self.vector_column}", TO_REAL_VECTOR '
            f"     (ARRAY({embedding_as_str}))) AS CS "  # row[3]
            f'FROM "{self.table_name}"'
        )
        order_str = f" order by CS {HANA_DISTANCE_FUNCTION[self.distance_strategy][1]}"
        where_str, query_tuple = self._create_where_by_filter(filter)
        sql_str = sql_str + where_str
        sql_str = sql_str + order_str
        try:
            cur = self.connection.cursor()
            cur.execute(sql_str, query_tuple)
            if cur.has_result_set():
                rows = cur.fetchall()
                for row in rows:
                    js = json.loads(row[1])
                    doc = Document(page_content=row[0], metadata=js)
                    result_vector = HanaDB._parse_float_array_from_string(row[2])
                    result.append((doc, row[3], result_vector))
        finally:
            cur.close()
        return result

    def similarity_search_with_score_by_vector(
        self, embedding: List[float], k: int = 4, filter: Optional[dict] = None
    ) -> List[Tuple[Document, float]]:
        """Return docs most similar to the given embedding.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filter: A dictionary of metadata fields and values to filter by.
                    Defaults to None.

        Returns:
            List of Documents most similar to the query and score for each
        """
        whole_result = self.similarity_search_with_score_and_vector_by_vector(
            embedding=embedding, k=k, filter=filter
        )
        return [(result_item[0], result_item[1]) for result_item in whole_result]

    def similarity_search_by_vector(  # type: ignore[override]
        self, embedding: List[float], k: int = 4, filter: Optional[dict] = None
    ) -> List[Document]:
        """Return docs most similar to embedding vector.

        Args:
            embedding: Embedding to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filter: A dictionary of metadata fields and values to filter by.
                    Defaults to None.

        Returns:
            List of Documents most similar to the query vector.
        """
        docs_and_scores = self.similarity_search_with_score_by_vector(
            embedding=embedding, k=k, filter=filter
        )
        return [doc for doc, _ in docs_and_scores]

    def _create_where_by_filter(self, filter):  # type: ignore[no-untyped-def]
        query_tuple = []
        where_str = ""
        if filter:
            where_str, query_tuple = self._process_filter_object(filter)
            where_str = " WHERE " + where_str
        return where_str, query_tuple

    def _process_filter_object(self, filter):  # type: ignore[no-untyped-def]
        query_tuple = []
        where_str = ""
        if filter:
            for i, key in enumerate(filter.keys()):
                filter_value = filter[key]
                if i != 0:
                    where_str += " AND "

                # Handling of 'special' boolean operators "$and", "$or"
                if key in LOGICAL_OPERATORS_TO_SQL:
                    logical_operator = LOGICAL_OPERATORS_TO_SQL[key]
                    logical_operands = filter_value
                    for j, logical_operand in enumerate(logical_operands):
                        if j != 0:
                            where_str += f" {logical_operator} "
                        (
                            where_str_logical,
                            query_tuple_logical,
                        ) = self._process_filter_object(logical_operand)
                        where_str += where_str_logical
                        query_tuple += query_tuple_logical
                    continue

                operator = "="
                sql_param = "?"

                if isinstance(filter_value, bool):
                    query_tuple.append("true" if filter_value else "false")
                elif isinstance(filter_value, int) or isinstance(filter_value, str):
                    query_tuple.append(filter_value)
                elif isinstance(filter_value, Dict):
                    # Handling of 'special' operators starting with "$"
                    special_op = next(iter(filter_value))
                    special_val = filter_value[special_op]
                    # "$eq", "$ne", "$lt", "$lte", "$gt", "$gte"
                    if special_op in COMPARISONS_TO_SQL:
                        operator = COMPARISONS_TO_SQL[special_op]
                        if isinstance(special_val, bool):
                            query_tuple.append("true" if filter_value else "false")
                        elif isinstance(special_val, float):
                            sql_param = "CAST(? as float)"
                            query_tuple.append(special_val)
                        else:
                            query_tuple.append(special_val)
                    # "$between"
                    elif special_op == BETWEEN_OPERATOR:
                        between_from = special_val[0]
                        between_to = special_val[1]
                        operator = "BETWEEN"
                        sql_param = "? AND ?"
                        query_tuple.append(between_from)
                        query_tuple.append(between_to)
                    # "$like"
                    elif special_op == LIKE_OPERATOR:
                        operator = "LIKE"
                        query_tuple.append(special_val)
                    # "$in", "$nin"
                    elif special_op in IN_OPERATORS_TO_SQL:
                        operator = IN_OPERATORS_TO_SQL[special_op]
                        if isinstance(special_val, list):
                            for i, list_entry in enumerate(special_val):
                                if i == 0:
                                    sql_param = "("
                                sql_param = sql_param + "?"
                                if i == (len(special_val) - 1):
                                    sql_param = sql_param + ")"
                                else:
                                    sql_param = sql_param + ","
                                query_tuple.append(list_entry)
                        else:
                            raise ValueError(
                                f"Unsupported value for {operator}: {special_val}"
                            )
                    else:
                        raise ValueError(f"Unsupported operator: {special_op}")
                else:
                    raise ValueError(
                        f"Unsupported filter data-type: {type(filter_value)}"
                    )
                # If the key is in data_column, we query a column
                if key in self.data_columns:
                    col_str = f''' "{key}" '''
                # Otherwise we extract the key from the JSON metadata column
                else:
                    col_str = f" JSON_VALUE({self.metadata_column}, '$.{key}')"
                where_str += (
                    col_str +
                    f" {operator} {sql_param}"
                )

        return where_str, query_tuple

    def delete(  # type: ignore[override]
        self, ids: Optional[List[str]] = None, filter: Optional[dict] = None
    ) -> Optional[bool]:
        """Delete entries by filter with metadata values

        Args:
            ids: Deletion with ids is not supported! A ValueError will be raised.
            filter: A dictionary of metadata fields and values to filter by.
                    An empty filter ({}) will delete all entries in the table.

        Returns:
            Optional[bool]: True, if deletion is technically successful.
            Deletion of zero entries, due to non-matching filters is a success.
        """
        # If the vectorstore is based on a view, we can't insert
        if self.read_only:
            raise Warning('The database object that contains the data is read only. Most probably it is a view.')

        if ids is not None:
            raise ValueError("Deletion via ids is not supported")

        if filter is None:
            raise ValueError("Parameter 'filter' is required when calling 'delete'")

        where_str, query_tuple = self._create_where_by_filter(filter)
        sql_str = f'DELETE FROM "{self.table_name}" {where_str}'

        try:
            cur = self.connection.cursor()
            cur.execute(sql_str, query_tuple)
        finally:
            cur.close()

        return True

    async def adelete(  # type: ignore[override]
        self, ids: Optional[List[str]] = None, filter: Optional[dict] = None
    ) -> Optional[bool]:
        """Delete by vector ID or other criteria.

        Args:
            ids: List of ids to delete.

        Returns:
            Optional[bool]: True if deletion is successful,
            False otherwise, None if not implemented.
        """
        return await run_in_executor(None, self.delete, ids=ids, filter=filter)

    def max_marginal_relevance_search(  # type: ignore[override]
        self,
        query: str,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        filter: Optional[dict] = None,
    ) -> List[Document]:
        """Return docs selected using the maximal marginal relevance.

        Maximal marginal relevance optimizes for similarity to query AND diversity
        among selected documents.

        Args:
            query: search query text.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                        of diversity among the results with 0 corresponding
                        to maximum diversity and 1 to minimum diversity.
                        Defaults to 0.5.
            filter: Filter on metadata properties, e.g.
                            {
                                "str_property": "foo",
                                "int_property": 123
                            }
        Returns:
            List of Documents selected by maximal marginal relevance.
        """
        embedding = self.embedding.embed_query(query)
        return self.max_marginal_relevance_search_by_vector(
            embedding=embedding,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
            filter=filter,
        )

    def _parse_float_array_from_string(array_as_string: str) -> List[float]:  # type: ignore[misc]
        array_wo_brackets = array_as_string[1:-1]
        return [float(x) for x in array_wo_brackets.split(",")]

    def max_marginal_relevance_search_by_vector(  # type: ignore[override]
        self,
        embedding: List[float],
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        filter: Optional[dict] = None,
    ) -> List[Document]:
        whole_result = self.similarity_search_with_score_and_vector_by_vector(
            embedding=embedding, k=fetch_k, filter=filter
        )
        embeddings = [result_item[2] for result_item in whole_result]
        mmr_doc_indexes = maximal_marginal_relevance(
            np.array(embedding), embeddings, lambda_mult=lambda_mult, k=k
        )

        return [whole_result[i][0] for i in mmr_doc_indexes]

    async def amax_marginal_relevance_search_by_vector(  # type: ignore[override]
        self,
        embedding: List[float],
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
    ) -> List[Document]:
        """Return docs selected using the maximal marginal relevance."""
        return await run_in_executor(
            None,
            self.max_marginal_relevance_search_by_vector,
            embedding=embedding,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
        )

    @staticmethod
    def _cosine_relevance_score_fn(distance: float) -> float:
        return distance

    def _select_relevance_score_fn(self) -> Callable[[float], float]:
        """
        The 'correct' relevance function
        may differ depending on a few things, including:
        - the distance / similarity metric used by the VectorStore
        - the scale of your embeddings (OpenAI's are unit normed. Many others are not!)
        - embedding dimensionality
        - etc.

        Vectorstores should define their own selection based method of relevance.
        """
        if self.distance_strategy == DistanceStrategy.COSINE:
            return HanaDB._cosine_relevance_score_fn
        elif self.distance_strategy == DistanceStrategy.EUCLIDEAN_DISTANCE:
            return HanaDB._euclidean_relevance_score_fn
        else:
            raise ValueError(
                "Unsupported distance_strategy: {}".format(self.distance_strategy)
            )
