#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(__GNUC__)
__attribute__((noinline))
#endif
static void trigger_heap_overflow(void) {
    volatile unsigned char *p = (volatile unsigned char *)malloc(4);
    if (p == NULL) {
        return;
    }
    p[0] = 0;
    p[4] = 1;  /* deliberate ASAN heap-buffer-overflow for K3 acceptance */
    free((void *)p);
}

static int run_input(const char *path) {
    unsigned char buf[16] = {0};
    FILE *fh = fopen(path, "rb");
    if (fh == NULL) {
        return 2;
    }
    size_t size = fread(buf, 1, sizeof(buf), fh);
    fclose(fh);
    if (size >= 7 && memcmp(buf, "K3CRASH", 7) == 0) {
        trigger_heap_overflow();
        return 0;
    }
    if (size >= 6 && memcmp(buf, "K3HANG", 6) == 0) {
        for (;;) {
        }
    }
    return 0;
}

int main(int argc, char **argv) {
    for (int i = 1; i < argc; ++i) {
        int rc = run_input(argv[i]);
        if (rc != 0) {
            return rc;
        }
    }
    return 0;
}
