FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.lock pyproject.toml ./
COPY trader ./trader
RUN python -m pip install --no-cache-dir --prefix=/install -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps --prefix=/install .

FROM python:3.12-slim
RUN groupadd --gid 10001 trader && useradd --uid 10001 --gid 10001 --no-create-home trader
WORKDIR /app
COPY --from=build /install /usr/local
COPY config ./config
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "trader"]
CMD ["paper", "--config", "config/local.yaml"]
