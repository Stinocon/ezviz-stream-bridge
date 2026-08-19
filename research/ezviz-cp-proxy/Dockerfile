ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3

COPY cp_proxy.py /usr/src/cp_proxy.py
COPY run.sh /usr/src/run.sh
RUN chmod +x /usr/src/run.sh /usr/src/cp_proxy.py

CMD [ "/usr/src/run.sh" ]
