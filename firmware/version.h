#ifndef FLOW_VERSION_H
#define FLOW_VERSION_H
/* major<<16 | minor<<8 | patch. Reported as the VIA firmware-version keyboard value and,
 * as the minor number, through custom value 15 (shown in the VIA menu). */
#define FLOW_VERSION_MAJOR 0
#define FLOW_VERSION_MINOR 2
#define FLOW_VERSION_PATCH 0
#define FLOW_FW_VERSION ((FLOW_VERSION_MAJOR<<16)|(FLOW_VERSION_MINOR<<8)|FLOW_VERSION_PATCH)
#endif
