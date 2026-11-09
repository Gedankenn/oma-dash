CC ?= docker
IMAGE ?= alpine:3.20
SRC = fbdash.c

all: fbdash

fbdash: $(SRC)
	docker run --rm --platform linux/arm/v7 -v "$$PWD":/src $(IMAGE) sh -c \
		'apk add --no-cache gcc musl-dev >/dev/null && gcc -O2 -static -o /src/fbdash /src/$(SRC)'

clean:
	rm -f fbdash

.PHONY: all clean
