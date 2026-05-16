from sqlalchemy.orm import Query


class SoftDeleteQuery(Query):
    """Default query filters out soft-deleted rows.

    Usage:
        User.query.all()              # deleted_at IS NULL
        User.query.with_deleted()     # all rows including deleted
        User.query.only_deleted()     # only soft-deleted rows
    """

    def __new__(cls, *args, **kwargs):
        obj = super().__new__(cls)
        obj._with_deleted = False
        return obj

    def _filter_deleted(self, query):
        if not self._with_deleted:
            from sqlalchemy import inspect

            mapper = self._entity_zero().mapper
            if hasattr(mapper.class_, "deleted_at"):
                query = query.filter(mapper.class_.deleted_at.is_(None))
        return query

    def with_deleted(self):
        obj = self._clone()
        obj._with_deleted = True
        return obj

    def only_deleted(self):
        obj = self._clone()
        obj._with_deleted = True
        from sqlalchemy import inspect

        mapper = self._entity_zero().mapper
        if hasattr(mapper.class_, "deleted_at"):
            return obj.filter(mapper.class_.deleted_at.isnot(None))
        return obj
