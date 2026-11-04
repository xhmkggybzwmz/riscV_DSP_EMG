/* c_sort.c - insertion sort and recursive quicksort of signed values. */
#include <stdint.h>
#include "rv32_soc.h"

static int32_t a[32] = {
    17, -3, 99, 0, -2147483647 - 1, 42, 7, 7, -100, 2147483647, 5, 3,
    1000, -1, 64, 63, 2, -50, 8, 9, 11, -11, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22
};
static int32_t b[32];

static void insertion_sort(int32_t *v, int n)
{
    for (int i = 1; i < n; i++) {
        int32_t key = v[i];
        int j = i - 1;
        while (j >= 0 && v[j] > key) {
            v[j + 1] = v[j];
            j--;
        }
        v[j + 1] = key;
    }
}

static void quicksort(int32_t *v, int lo, int hi)
{
    if (lo >= hi)
        return;
    int32_t pivot = v[(lo + hi) / 2];
    int i = lo, j = hi;
    while (i <= j) {
        while (v[i] < pivot) i++;
        while (v[j] > pivot) j--;
        if (i <= j) {
            int32_t t = v[i]; v[i] = v[j]; v[j] = t;
            i++; j--;
        }
    }
    quicksort(v, lo, j);
    quicksort(v, i, hi);
}

int main(void)
{
    for (int i = 0; i < 32; i++)
        b[i] = a[i];
    insertion_sort(a, 32);
    quicksort(b, 0, 31);
    for (int i = 0; i < 31; i++)
        CHECK(a[i] <= a[i + 1], 1);
    for (int i = 0; i < 32; i++)
        CHECK(a[i] == b[i], 2);
    CHECK(a[0] == -2147483647 - 1, 3);
    CHECK(a[31] == 2147483647, 4);
    CHECK(a[1] == -100 && a[2] == -50, 5);
    return 0;
}
