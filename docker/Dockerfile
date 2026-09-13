# Matches the Python that CI runs (.github/workflows/ci.yml). If one moves the
# other has to, or the image ships a runtime the test suite never saw.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# No gcc, and no libpq-dev. Every dependency that used to need them now ships a
# manylinux wheel -- psycopg2-binary most of all, which is the reason libpq-dev
# was here. Verified by building this image: pip resolves every requirement to
# a wheel and compiles nothing.
#
# Leaving a compiler in a runtime image is not a size problem, it is a reach
# problem: anything that gets code execution in the container gets a toolchain
# with it.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# A fixed uid, not a name. Docker seeds a fresh named volume from the image's
# directory *and its ownership*, so `uploads` comes up writable only because
# this directory exists here owned by this user. A uid that moved between
# builds would leave an existing volume owned by a user that no longer exists.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/app/core/static/uploads

COPY --chown=app:app . .

RUN chmod +x docker/entrypoint.sh \
    && chown -R app:app /app/app/core/static/uploads

USER app

EXPOSE 5000

ENTRYPOINT ["docker/entrypoint.sh"]
