int partialsum (float q[], int size, float epsilon)
{
  "TYPES: epsilon: <0, 0>; size: <0, 0>; q: <*, *>;";
  "PRECONDITION: ONE_DIFFER;";
  "CHECK: epsilon";

  float sum = 0; int i = 0;
  while(i < size)
  {
    sum = sum + q[i];
    i = i + 1;
  }
  float eta = Lap(1 / epsilon);
  CHECKDP_OUTPUT(sum + eta);
}